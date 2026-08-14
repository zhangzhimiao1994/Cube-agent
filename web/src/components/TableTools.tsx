import type { ReactNode } from "react";

export type SortDirection = "asc" | "desc";

export type SortState<Key extends string> = {
  direction: SortDirection;
  key: Key;
};

export function nextSortState<Key extends string>(current: SortState<Key>, key: Key): SortState<Key> {
  if (current.key !== key) return { key, direction: "asc" };
  return { key, direction: current.direction === "asc" ? "desc" : "asc" };
}

export function compareText(left: string, right: string, direction: SortDirection) {
  const result = left.localeCompare(right, "zh-Hans-CN", { numeric: true, sensitivity: "base" });
  return direction === "asc" ? result : -result;
}

export function textContains(value: string, query: string) {
  const normalized = query.trim().toLowerCase();
  return !normalized || value.toLowerCase().includes(normalized);
}

type SortHeaderProps<Key extends string> = {
  children: ReactNode;
  column: Key;
  label: string;
  onSort: (column: Key) => void;
  sort: SortState<Key>;
};

export function SortHeader<Key extends string>({ children, column, label, onSort, sort }: SortHeaderProps<Key>) {
  const active = sort.key === column;
  return (
    <button
      type="button"
      className={active ? "table-sort-button table-sort-active" : "table-sort-button"}
      aria-label={`${label}排序`}
      onClick={() => onSort(column)}
    >
      <span>{children}</span>
      <span aria-hidden="true" className="table-sort-indicator">
        {active ? (sort.direction === "asc" ? "↑" : "↓") : "↕"}
      </span>
    </button>
  );
}