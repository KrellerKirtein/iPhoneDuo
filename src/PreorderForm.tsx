import { useEffect, useState } from "react";
import type { Action, Data } from "./types";

/** 将用户预先授权的购买范围传给账号独占任务，时间固定按北京时间解释。 */
export function PreorderForm({
  sku,
  busy,
  start,
}: {
  sku: string;
  busy: boolean;
  start: (action: Action, params?: Data) => Promise<void>;
}) {
  const [target, setTarget] = useState("2026-10-16T20:00");
  const [maximum, setMaximum] = useState("");
  const [stores, setStores] = useState("");
  const [suffix, setSuffix] = useState("");
  const [authorized, setAuthorized] = useState(false);
  const [windowMinutes, setWindowMinutes] = useState(5);
  const [error, setError] = useState("");
  useEffect(() => setAuthorized(false), [sku]);

  return (
    <section className="section">
      <div className="section-title">
        <h2>定时预购</h2>
        <span>送货优先 · 自动建单 · 手动付款</span>
      </div>
      <p className="muted">
        购买上方 SKU 的 1
        件商品。先在“本地配置”为当前账号保存完整联系人和送货地址。
        程序会提前检查登录；等待期间请保持应用运行、电脑联网且不休眠。
      </p>
      <form
        onSubmit={(event) => {
          event.preventDefault();
          const pickupStores = stores.split(/[\s,，]+/).filter(Boolean);
          if (pickupStores.some((store) => !/^R\d{3,5}$/.test(store))) {
            setError("请填写 R 开头的门店编号，用逗号分隔。");
            return;
          }
          setError("");
          void start("preorder", {
            sku,
            quantity: 1,
            target_at: `${target}:00+08:00`,
            max_total: maximum,
            window_seconds: windowMinutes * 60,
            pickup_stores: pickupStores,
            id_suffix: suffix,
            authorized,
          });
        }}
      >
        <div className="preorder-fields">
          <label>
            开售时间（北京时间）
            <input
              type="datetime-local"
              required
              value={target}
              onChange={(event) => {
                setTarget(event.target.value);
                setAuthorized(false);
              }}
            />
          </label>
          <label>
            订单总额上限（人民币元）
            <input
              type="number"
              min="0.01"
              step="0.01"
              required
              value={maximum}
              onChange={(event) => {
                setMaximum(event.target.value);
                setAuthorized(false);
              }}
            />
          </label>
          <label>
            开售后执行窗口（分钟）
            <input
              type="number"
              min={1}
              max={15}
              required
              value={windowMinutes}
              onChange={(event) => {
                setWindowMinutes(Number(event.target.value));
                setAuthorized(false);
              }}
            />
          </label>
          <label>
            备选门店编号（按优先顺序，可留空）
            <input
              value={stores}
              placeholder="R390, R401"
              onChange={(event) => {
                setStores(event.target.value.toUpperCase());
                setAuthorized(false);
              }}
            />
          </label>
          {stores.trim() && (
            <label>
              取货人证件后四位
              <input
                required
                maxLength={4}
                minLength={4}
                pattern="[0-9A-Za-z]{4}"
                autoComplete="off"
                value={suffix}
                onChange={(event) => {
                  setSuffix(event.target.value.toUpperCase());
                  setAuthorized(false);
                }}
              />
            </label>
          )}
        </div>
        <p className="muted">
          仅在官网明确没有可用配送服务时尝试备选门店。执行窗口结束后停止。
          需要取货预约时，选择官网返回的首个可用时段。
          提交结果不明时只查单，不重新购买；重启后点击“恢复已保存计划”。
        </p>
        <label className="preorder-consent">
          <input
            type="checkbox"
            checked={authorized}
            onChange={(event) => setAuthorized(event.target.checked)}
          />
          我授权按以上商品、地址和金额上限自动创建一笔订单，付款由我完成
        </label>
        {error && <p role="alert">{error}</p>}
        <div className="button-row">
          <button
            className="primary"
            type="submit"
            disabled={busy || !authorized || !sku.trim()}
          >
            启用定时预购
          </button>
          <button
            type="button"
            disabled={busy}
            onClick={() => void start("preorder_resume")}
          >
            恢复已保存计划
          </button>
          <button
            type="button"
            disabled={busy}
            onClick={() => void start("preorder_status")}
          >
            查看计划状态
          </button>
        </div>
      </form>
    </section>
  );
}
