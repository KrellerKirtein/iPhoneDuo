// Product presets. Prices are reference values, never checkout quotes.
export const duoProducts = [
  { sku: "MK2M4CH", name: "iPhone Duo 256GB Star White", price: 15999 },
  { sku: "MK2P4CH", name: "iPhone Duo 512GB Star White", price: 17999 },
  { sku: "MK2T4CH", name: "iPhone Duo 1TB Star White", price: 21499 },
  { sku: "MK2V4CH", name: "iPhone Duo 2TB Star White", price: 26499 },
  { sku: "MK2N4CH", name: "iPhone Duo 256GB Night Sky", price: 15999 },
  { sku: "MK2Q4CH", name: "iPhone Duo 512GB Night Sky", price: 17999 },
  { sku: "MK2U4CH", name: "iPhone Duo 1TB Night Sky", price: 21499 },
  { sku: "MK2W4CH", name: "iPhone Duo 2TB Night Sky", price: 26499 },
];
export const presetProducts = [
  ...duoProducts,
  // Apple CN catalog verified on 2026-10-13; checkout normalizes the /A suffix.
  { sku: "MJY64CH", name: "iPhone 18 Pro Max 256GB 黑色", price: 10999 },
  { sku: "MLH74FE", name: "抛光布", price: 69 },
];
export const priceText = (value: unknown) =>
  value != null && value !== "" && Number.isFinite(Number(value))
    ? `${Number(value).toLocaleString("zh-CN", { minimumFractionDigits: 2, maximumFractionDigits: 2 })} CNY`
    : "未知";
