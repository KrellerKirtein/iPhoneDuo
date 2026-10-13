import { useId, useState } from "react";
import { retailStoreCatalog } from "./retail-stores";

/** 离线查询官网门店编号，按点击顺序追加备选门店，不触发账号任务。 */
export function StoreLookup({
  value,
  onChange,
}: {
  value: string;
  onChange: (value: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const id = useId();
  const selected = value
    .toUpperCase()
    .split(/[\s,，]+/)
    .filter(Boolean);
  const terms = query.trim().toLowerCase().split(/\s+/).filter(Boolean);
  const matches = retailStoreCatalog.stores.filter((store) => {
    const text =
      `${store.id} ${store.name} ${store.province} ${store.city} ${store.address}`.toLowerCase();
    return terms.every((term) => text.includes(term));
  });

  return (
    <div className="store-lookup">
      <button
        type="button"
        aria-expanded={open}
        aria-controls={id}
        onClick={() => setOpen(!open)}
      >
        {open ? "收起门店查询" : "查询门店编号"}
      </button>
      {open && (
        <div id={id} className="store-lookup-panel">
          <label>
            搜索门店（城市、店名或编号）
            <input
              type="search"
              value={query}
              placeholder="例如：上海、香港广场、R390"
              onChange={(event) => setQuery(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === "Enter") event.preventDefault();
              }}
            />
          </label>
          <p className="muted">
            中国大陆门店目录 · 更新于 {retailStoreCatalog.updatedAt}。
            查询编号不代表有货；请选择送货地址所在城市的门店。
            点击“添加”按优先顺序填入，最多 10 家。
          </p>
          <p role="status">
            {matches.length
              ? `找到 ${matches.length} 家门店`
              : "未找到匹配门店，请尝试城市、店名或编号；也可手动填写编号。"}
            {selected.length >= 10 &&
              "；已达 10 家上限，请先在编号输入框中移除门店。"}
          </p>
          <ul className="store-lookup-results" aria-label="门店查询结果">
            {matches.map((store) => {
              const added = selected.includes(store.id);
              return (
                <li key={store.id}>
                  <div>
                    <strong>
                      {store.city} · {store.name}
                    </strong>
                    <span className="store-lookup-code">{store.id}</span>
                    <p className="muted">{store.address}</p>
                  </div>
                  <button
                    type="button"
                    disabled={added || selected.length >= 10}
                    aria-label={`${added ? "已添加" : "添加"} ${store.name} ${store.id}`}
                    onClick={() => onChange([...selected, store.id].join(", "))}
                  >
                    {added ? "已添加" : "添加"}
                  </button>
                </li>
              );
            })}
          </ul>
        </div>
      )}
    </div>
  );
}
