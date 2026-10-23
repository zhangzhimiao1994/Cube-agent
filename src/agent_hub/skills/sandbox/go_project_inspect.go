// Trusted source inventory only. This program never compiles or executes project code.
// The caller authenticates this helper, the SDK/GOROOT, and the sealed complete root.
package main

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"go/ast"
	"go/build"
	"go/parser"
	"go/token"
	"io"
	"io/fs"
	"os"
	"path/filepath"
	"regexp"
	"runtime"
	"sort"
	"strconv"
	"strings"
	"time"
	"unicode"
	"unicode/utf8"
)

type options struct {
	root, module string
	maxFiles     int
	maxPackages  int
	maxTests     int
	maxBytes     int64
	timeout      time.Duration
}

type testID struct {
	Package string `json:"package"`
	Name    string `json:"name"`
}

type sourceFile struct {
	Path      string `json:"path"`
	RawSHA256 string `json:"raw_sha256"`
	SizeBytes int    `json:"size_bytes"`
}

type inventory struct {
	SchemaVersion      int          `json:"schema_version"`
	Kind               string       `json:"kind"`
	ModulePath         string       `json:"module_path"`
	Packages           []string     `json:"packages"`
	ExpectedTests      []testID     `json:"expected_tests"`
	ProjectMainPackage string       `json:"project_main_package"`
	SourceGoFiles      []sourceFile `json:"source_go_files"`
}

var pathPattern = regexp.MustCompile(`^[A-Za-z0-9_~+.-]+(/[A-Za-z0-9_~+.-]+)*$`)

// Match the first recipe's whole-snapshot pure-Go policy, not just active Go files.
var unsupportedNativeSuffixes = [...]string{
	".c", ".cc", ".cpp", ".cxx", ".h", ".hh", ".hpp", ".s", ".syso", ".m",
	".f", ".for", ".f90", ".swig", ".swigcxx",
}

func reject(code string) error { return errors.New(code) }

func nativeSource(name string) bool {
	extension := filepath.Ext(name)
	for _, suffix := range unsupportedNativeSuffixes {
		if strings.EqualFold(extension, suffix) {
			return true
		}
	}
	return false
}

func safePath(path string) bool {
	if len(path) == 0 || len(path) > 1024 || !pathPattern.MatchString(path) {
		return false
	}
	for _, part := range strings.Split(path, "/") {
		if strings.HasPrefix(part, ".") || strings.HasSuffix(part, ".") {
			return false
		}
	}
	return true
}

func arguments(args []string) (options, error) {
	var out options
	if len(args) != 14 {
		return out, reject("go_inventory_arguments_invalid")
	}
	values := map[string]string{}
	for i := 0; i < len(args); i += 2 {
		key := args[i]
		switch key {
		case "--root", "--module", "--max-files", "--max-bytes", "--timeout-ms", "--max-packages", "--max-tests":
		default:
			return out, reject("go_inventory_arguments_invalid")
		}
		if _, exists := values[key]; exists {
			return out, reject("go_inventory_arguments_invalid")
		}
		values[key] = args[i+1]
	}
	if len(values) != 7 {
		return out, reject("go_inventory_arguments_invalid")
	}
	out.root, out.module = values["--root"], values["--module"]
	n, e1 := strconv.Atoi(values["--max-files"])
	b, e2 := strconv.ParseInt(values["--max-bytes"], 10, 64)
	t, e3 := strconv.ParseInt(values["--timeout-ms"], 10, 64)
	p, e4 := strconv.Atoi(values["--max-packages"])
	x, e5 := strconv.Atoi(values["--max-tests"])
	if e1 != nil || e2 != nil || e3 != nil || e4 != nil || e5 != nil || p < 1 || x < 1 || n < 1 || n > 100000 || b < 1 || b > 500*1024*1024 ||
		t < 1 || t > 3600000 || !filepath.IsAbs(out.root) || filepath.Clean(out.root) != out.root || !safePath(out.module) {
		return out, reject("go_inventory_arguments_invalid")
	}
	out.maxFiles, out.maxBytes, out.timeout = n, b, time.Duration(t)*time.Millisecond
	out.maxPackages, out.maxTests = p, x
	return out, nil
}

type snapshot struct {
	opts       options
	until      time.Time
	files      map[string][]byte
	dirs       map[string][]fs.FileInfo
	stamps     map[string]fs.FileInfo
	spellings  map[string]bool
	identities map[int64][]fs.FileInfo
	entries    int
	remaining  int64
}

func (s *snapshot) check() error {
	if !time.Now().Before(s.until) {
		return reject("go_inventory_timeout")
	}
	return nil
}

func unchanged(a, b fs.FileInfo) bool {
	return os.SameFile(a, b) && a.Mode() == b.Mode() && a.Size() == b.Size() && a.ModTime() == b.ModTime()
}

func (s *snapshot) capture(dir string, depth int) error {
	if err := s.check(); err != nil {
		return err
	}
	if depth > 64 {
		return reject("go_inventory_limit")
	}
	before, err := os.Lstat(dir)
	if err != nil || !before.IsDir() || before.Mode()&os.ModeSymlink != 0 {
		return reject("go_inventory_source_unsafe")
	}
	f, err := os.Open(dir)
	if err != nil {
		return reject("go_inventory_source_unsafe")
	}
	defer f.Close()
	opened, err := f.Stat()
	if err != nil || !unchanged(before, opened) {
		return reject("go_inventory_source_unsafe")
	}
	s.stamps[dir] = before
	s.dirs[dir] = []fs.FileInfo{}
	for {
		entries, readErr := f.ReadDir(64)
		if readErr != nil && readErr != io.EOF {
			return reject("go_inventory_source_unsafe")
		}
		for _, entry := range entries {
			if err := s.check(); err != nil {
				return err
			}
			s.entries++
			if s.entries > s.opts.maxFiles {
				return reject("go_inventory_limit")
			}
			path := filepath.Join(dir, entry.Name())
			rel, err := filepath.Rel(s.opts.root, path)
			if err != nil || !safePath(filepath.ToSlash(rel)) {
				return reject("go_inventory_source_unsafe")
			}
			folded := strings.ToLower(filepath.ToSlash(rel))
			if s.spellings[folded] {
				return reject("go_inventory_duplicate")
			}
			s.spellings[folded] = true
			info, err := os.Lstat(path)
			if err != nil || info.Mode()&os.ModeSymlink != 0 {
				return reject("go_inventory_source_unsafe")
			}
			s.dirs[dir] = append(s.dirs[dir], info)
			if info.IsDir() {
				if err := s.capture(path, depth+1); err != nil {
					return err
				}
				continue
			}
			if !info.Mode().IsRegular() {
				return reject("go_inventory_source_unsafe")
			}
			if nativeSource(entry.Name()) {
				return reject("go_inventory_native_source_unsupported")
			}
			for _, prior := range s.identities[info.Size()] {
				if err := s.check(); err != nil {
					return err
				}
				if os.SameFile(prior, info) {
					return reject("go_inventory_duplicate")
				}
			}
			s.identities[info.Size()] = append(s.identities[info.Size()], info)
			if info.Size() > s.remaining {
				return reject("go_inventory_limit")
			}
			file, err := os.Open(path)
			if err != nil {
				return reject("go_inventory_source_unsafe")
			}
			data, readErr := io.ReadAll(io.LimitReader(file, s.remaining+1))
			after, statErr := file.Stat()
			file.Close()
			if readErr != nil || statErr != nil || !unchanged(info, after) || int64(len(data)) != info.Size() {
				return reject("go_inventory_source_unsafe")
			}
			s.remaining -= int64(len(data))
			if s.remaining < 0 {
				return reject("go_inventory_limit")
			}
			s.files[path], s.stamps[path] = data, info
			name := strings.ToLower(entry.Name())
			if name == "go.work" || name == "go.work.sum" || name == "go.mod" && dir != s.opts.root {
				return reject("go_inventory_module_rejected")
			}
		}
		if readErr == io.EOF {
			break
		}
	}
	sort.Slice(s.dirs[dir], func(i, j int) bool { return s.dirs[dir][i].Name() < s.dirs[dir][j].Name() })
	return nil
}

func (s *snapshot) context() build.Context {
	ctx := build.Context{GOOS: "linux", GOARCH: "amd64", CgoEnabled: false, Compiler: "gc",
		GOROOT: runtime.GOROOT(), ReleaseTags: append([]string{}, build.Default.ReleaseTags...)}
	for _, tag := range build.Default.ToolTags {
		if strings.HasPrefix(tag, "goexperiment.") {
			ctx.ToolTags = append(ctx.ToolTags, tag)
		}
	}
	ctx.ToolTags = append(ctx.ToolTags, "amd64.v1")
	// Custom snapshot callbacks also disable go/build's external go-command fallback.
	ctx.IsDir = func(path string) bool { _, ok := s.dirs[filepath.Clean(path)]; return ok }
	ctx.ReadDir = func(path string) ([]fs.FileInfo, error) {
		if err := s.check(); err != nil {
			return nil, err
		}
		rows, ok := s.dirs[filepath.Clean(path)]
		if !ok {
			return nil, os.ErrNotExist
		}
		return append([]fs.FileInfo{}, rows...), nil
	}
	ctx.OpenFile = func(path string) (io.ReadCloser, error) {
		if err := s.check(); err != nil {
			return nil, err
		}
		data, ok := s.files[filepath.Clean(path)]
		if !ok {
			return nil, os.ErrNotExist
		}
		return io.NopCloser(bytes.NewReader(data)), nil
	}
	return ctx
}

// Match cmd/go/internal/load's official top-level Test discovery rules.
func isTest(name string) bool {
	if !strings.HasPrefix(name, "Test") {
		return false
	}
	if name == "Test" {
		return true
	}
	r, _ := utf8.DecodeRuneInString(name[4:])
	return !unicode.IsLower(r)
}

func testSignature(fn *ast.FuncDecl) bool {
	t := fn.Type
	if t.TypeParams != nil || t.Results != nil && len(t.Results.List) != 0 || t.Params == nil ||
		len(t.Params.List) != 1 || len(t.Params.List[0].Names) > 1 {
		return false
	}
	ptr, ok := t.Params.List[0].Type.(*ast.StarExpr)
	if !ok {
		return false
	}
	if name, ok := ptr.X.(*ast.Ident); ok {
		return name.Name == "T"
	}
	sel, ok := ptr.X.(*ast.SelectorExpr)
	return ok && sel.Sel.Name == "T"
}

func (s *snapshot) inspect() (inventory, error) {
	out := inventory{1, "go_source_inventory_v1", s.opts.module, []string{}, []testID{}, s.opts.module, []sourceFile{}}
	parsed := map[string]*ast.File{}
	// v1 intentionally rejects unsupported syntax/TestMain in inactive Go files too.
	for path, data := range s.files {
		if err := s.check(); err != nil {
			return out, err
		}
		if !strings.HasSuffix(path, ".go") {
			continue
		}
		file, err := parser.ParseFile(token.NewFileSet(), "source.go", data, parser.SkipObjectResolution|parser.AllErrors)
		if err != nil {
			return out, reject("go_inventory_syntax_invalid")
		}
		for _, decl := range file.Decls {
			if fn, ok := decl.(*ast.FuncDecl); ok && fn.Recv == nil && fn.Name.Name == "TestMain" {
				return out, reject("go_inventory_testmain_forbidden")
			}
		}
		for _, spec := range file.Imports {
			name, err := strconv.Unquote(spec.Path.Value)
			own := name == s.opts.module || strings.HasPrefix(name, s.opts.module+"/")
			if err == nil && (name == "C" || name == "runtime/cgo") {
				return out, reject("go_inventory_native_import_unsupported")
			}
			if err != nil || !safePath(name) ||
				!own && strings.Contains(strings.Split(name, "/")[0], ".") {
				return out, reject("go_inventory_import_rejected")
			}
		}
		parsed[path] = file
	}
	ctx := s.context()
	directories := []string{}
	for dir := range s.dirs {
		directories = append(directories, dir)
	}
	sort.Strings(directories)
	seenTests := map[testID]bool{}
	for _, dir := range directories {
		if err := s.check(); err != nil {
			return out, err
		}
		rel, _ := filepath.Rel(s.opts.root, dir)
		ignored := false
		if rel != "." {
			for _, part := range strings.Split(filepath.ToSlash(rel), "/") {
				if part == "testdata" || part == "vendor" || strings.HasPrefix(part, "_") {
					ignored = true
				}
			}
		}
		if ignored {
			continue
		}
		pkg, err := ctx.ImportDir(dir, 0)
		if dir == s.opts.root && pkg.Name != "main" {
			return out, reject("go_inventory_main_required")
		}
		if err != nil {
			var noGo *build.NoGoError
			if errors.As(err, &noGo) {
				continue
			}
			return out, reject("go_inventory_package_invalid")
		}
		id := s.opts.module
		if rel != "." {
			id += "/" + filepath.ToSlash(rel)
		}
		out.Packages = append(out.Packages, id)
		if len(out.Packages) > s.opts.maxPackages {
			return out, reject("go_inventory_limit")
		}
		count := 0
		for _, group := range [][]string{pkg.GoFiles, pkg.TestGoFiles, pkg.XTestGoFiles} {
			for _, name := range group {
				path := filepath.Join(dir, name)
				file, ok := parsed[path]
				if !ok {
					return out, reject("go_inventory_source_unsafe")
				}
				data := s.files[path]
				digest := sha256.Sum256(data)
				sourceRel, _ := filepath.Rel(s.opts.root, path)
				out.SourceGoFiles = append(out.SourceGoFiles, sourceFile{filepath.ToSlash(sourceRel), hex.EncodeToString(digest[:]), len(data)})
				if !strings.HasSuffix(name, "_test.go") {
					continue
				}
				for _, decl := range file.Decls {
					fn, ok := decl.(*ast.FuncDecl)
					if !ok || fn.Recv != nil || !isTest(fn.Name.Name) {
						continue
					}
					if !testSignature(fn) {
						return out, reject("go_inventory_test_invalid")
					}
					test := testID{id, fn.Name.Name}
					if seenTests[test] {
						return out, reject("go_inventory_duplicate")
					}
					seenTests[test] = true
					out.ExpectedTests = append(out.ExpectedTests, test)
					if len(out.ExpectedTests) > s.opts.maxTests {
						return out, reject("go_inventory_limit")
					}
					count++
				}
			}
		}
		if count == 0 {
			return out, reject("go_inventory_tests_missing")
		}
	}
	if len(out.Packages) == 0 {
		return out, reject("go_inventory_main_required")
	}
	packages := map[string]bool{}
	for _, id := range out.Packages {
		packages[id] = true
	}
	for _, source := range out.SourceGoFiles {
		for _, spec := range parsed[filepath.Join(s.opts.root, filepath.FromSlash(source.Path))].Imports {
			name, _ := strconv.Unquote(spec.Path.Value)
			if (name == s.opts.module || strings.HasPrefix(name, s.opts.module+"/")) && !packages[name] {
				return out, reject("go_inventory_import_rejected")
			}
		}
	}
	sort.Strings(out.Packages)
	sort.Slice(out.ExpectedTests, func(i, j int) bool {
		a, b := out.ExpectedTests[i], out.ExpectedTests[j]
		return a.Package < b.Package || a.Package == b.Package && a.Name < b.Name
	})
	sort.Slice(out.SourceGoFiles, func(i, j int) bool { return out.SourceGoFiles[i].Path < out.SourceGoFiles[j].Path })
	for path, before := range s.stamps {
		if err := s.check(); err != nil {
			return out, err
		}
		after, err := os.Lstat(path)
		if err != nil || !unchanged(before, after) {
			return out, reject("go_inventory_source_unsafe")
		}
	}
	return out, s.check()
}

func run() error {
	opts, err := arguments(os.Args[1:])
	if err != nil {
		return err
	}
	until := time.Now().Add(opts.timeout)
	// Bound parser time even inside a single large syntax tree or directory read.
	timer := time.AfterFunc(opts.timeout, func() { fmt.Fprintln(os.Stderr, "go_inventory_timeout"); os.Exit(2) })
	defer timer.Stop()
	for path := opts.root; ; path = filepath.Dir(path) {
		info, err := os.Lstat(path)
		if err != nil || !info.IsDir() || info.Mode()&os.ModeSymlink != 0 {
			return reject("go_inventory_source_unsafe")
		}
		if filepath.Dir(path) == path {
			break
		}
	}
	s := snapshot{opts: opts, until: until, files: map[string][]byte{}, dirs: map[string][]fs.FileInfo{},
		stamps: map[string]fs.FileInfo{}, spellings: map[string]bool{}, identities: map[int64][]fs.FileInfo{}, remaining: opts.maxBytes}
	if err := s.capture(opts.root, 0); err != nil {
		return err
	}
	out, err := s.inspect()
	if err != nil {
		return err
	}
	data, err := json.Marshal(out)
	if err != nil {
		return reject("go_inventory_invalid")
	}
	if err := s.check(); err != nil {
		return err
	}
	timer.Stop()
	if _, err := os.Stdout.Write(data); err != nil {
		return reject("go_inventory_output_failed")
	}
	return nil
}

func main() {
	defer func() {
		if recover() != nil {
			fmt.Fprintln(os.Stderr, "go_inventory_invalid")
			os.Exit(2)
		}
	}()
	if err := run(); err != nil {
		fmt.Fprintln(os.Stderr, err.Error())
		os.Exit(2)
	}
}
