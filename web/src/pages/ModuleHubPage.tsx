import { Link } from "react-router-dom";

import type { ModuleGroup } from "../app/navigation";
import { useAuth } from "../auth/AuthProvider";

export function ModuleHubPage({ group }: { group: ModuleGroup }) {
  const auth = useAuth();
  const modules = group.modules.filter((item) => auth.hasPermission(item.permission));

  return (
    <section className={`module-hub module-hub-${group.tone}`}>
      <p className="eyebrow">{group.eyebrow}</p>
      <h2>{group.label}</h2>
      <p className="compact-page-intro">{group.description}</p>

      <div className="module-card-grid" role="list" aria-label={`${group.label}模块`}>
        {modules.map((module, index) => (
          <Link
            key={module.to}
            to={module.to}
            className={`module-entry-card module-entry-card-${group.tone}`}
            style={{ "--module-index": index } as React.CSSProperties}
          >
            <span className="module-entry-number">{String(index + 1).padStart(2, "0")}</span>
            <strong>{module.label}</strong>
            <p>{module.description}</p>
          </Link>
        ))}
      </div>

      {modules.length === 0 ? (
        <article className="empty-state">
          <h3>暂无可访问模块</h3>
          <p>当前账号没有该分类下的权限，可以联系管理员调整权限。</p>
        </article>
      ) : null}
    </section>
  );
}
