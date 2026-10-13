# 分期建单与商品预设

目标：普通购买、购物袋结算和定时预购均可选择招商银行分期或支付宝信用卡分期（使用农业银行信用卡）。新增方式只创建待付款订单，用户随后在 Apple 订单页完成付款；项目不处理银行卡、银行认证、支付宝选卡、扣款或二维码。原有支付宝方式保留。

界面传递 `payment_method`（`alipay` / `cmb_installment` / `alipay_abc_installment`）和 `installment_term`。分期期数由用户选择，运行时须出现在官网当前订单的可用列表中。付款机构通过官网 billing options 或银行查询选项选择，沿用官网 action、字段和值；支付宝农业银行卡由用户在后续支付宝付款时手动选择，本项目不保证个人卡片资格。

Worker 在加购前校验参数，结账时选择机构及期数，复核页核对分期机构与期数，再按既有确认机制提交。新增分期结果只保存订单号、金额及付款方式说明，不请求或保留外部支付链接。定时计划冻结选择；变更付款方式/期数撤销界面授权。旧调用和旧计划缺省保留支付宝行为。Rust 参数透传，无需修改持久化格式版本。

失败边界：机构、期数、复核信息不支持或无法唯一识别时停止，不能回退到一次性付款；提交后结果不明沿用已提交标记和定时计划查单保护，不能因缺少二维码再次建单。恢复旧计划不读取界面当前选择。撤销本功能可恢复旧代码；已创建订单仍须从官网处理，不自动取消或重建。

商品预设：`iPhone 18 Pro Max 256GB 黑色`，真实 SKU `MJY64CH/A`，参考价 `10,999 元`。2026-10-13 从 [Apple 中国选购页](https://www.apple.com.cn/shop/buy-iphone/iphone-18-pro) 的商品 metrics 和 `PRODUCT_SELECTION_BOOTSTRAP` 交叉核对，并确认[该配置商品页](https://www.apple.com.cn/shop/buy-iphone/iphone-18-pro/mjy64ch/a)标题一致。界面沿用现有惯例保存 `MJY64CH`，下单时由 Worker 统一补全 `/A`。该项走正常购买流程，可用于真实下单；实际库存、配送和成交金额仍由结账响应确认。本次查询不执行加购或创建订单。

验证里程碑：前端构建与合成界面验证参数/授权/结果展示；离线 Worker 验证机构、期数、复核失败、仅一次提交、不访问支付平台及旧计划兼容；检查最终增量。测试不执行真实加购、建单或付款。

接口依据：2026-10-13 只读获取的 Apple `rs-checkout-5.18.11-16f6e/dist/checkout.js` 的 `billingOptions`、`bankLookUp` 元数据及 `selectedBillingOptions.installments.installmentOptions.selectInstallmentOption`；结果使用 `payment-instructions.js` 的 `externalPayAuth` / `thankYouInterstitial`。同日读取登录后的招商银行 24 期复核页，确认中文页面实际使用 `billingReview.installmentReview.d` 的 `labelImageAlt`（机构）、`installment`（分期标记）和 `description`（例如“24 个月，每月约 RMB 534”），已将脱敏字段保存到 `tests/worker/fixtures/cmb-24-review.json`。复核同时兼容原有 `bankName` / `paymentType` / `creditInstallmentTerm` 格式；文案无法完整识别、字段冲突或多条付款信息均停止建单。真实复核样本及离线测试不能代替最终建单、付款验证。购买规则见 [Apple 付款帮助](https://www.apple.com.cn/shop/help/payments)。

同日读取支付宝农行 24 期的真实复核页，官网将 `labelImageAlt: 中国农业银行` 与 `paymentProviderLabel: 通过支付宝` 分开返回，脱敏样本保存到 `tests/worker/fixtures/abc-24-review.json`。复核分别验证银行、付款渠道、分期标记和期数；存在独立渠道字段时必须匹配，不从 `appInstructions` 等说明文案推断渠道。普通结算和定时预购均使用此校验；仅确认复核信息，不执行真实建单或付款来验证。
