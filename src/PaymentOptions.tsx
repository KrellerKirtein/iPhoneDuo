import { SelectField } from "./SelectField";

export type PaymentMethod =
  | "alipay"
  | "cmb_installment"
  | "alipay_abc_installment";
export const paymentLabels: Record<PaymentMethod, string> = {
  alipay: "支付宝（原有方式）",
  cmb_installment: "招商银行分期（手动付款）",
  alipay_abc_installment: "支付宝·农业银行分期（手动付款）",
};

/** 只配置建单时的分期方式，银行卡选择和实际付款在项目外完成。 */
export function PaymentOptions({
  method,
  term,
  disabled,
  onMethod,
  onTerm,
}: {
  method: PaymentMethod;
  term: number;
  disabled: boolean;
  onMethod: (method: PaymentMethod) => void;
  onTerm: (term: number) => void;
}) {
  return (
    <div>
      <SelectField
        label="付款方式"
        value={method}
        disabled={disabled}
        options={Object.entries(paymentLabels).map(([value, label]) => ({
          value,
          label,
        }))}
        onChange={(value) => onMethod(value as PaymentMethod)}
      />
      {method !== "alipay" && (
        <>
          <SelectField
            label="分期期数"
            value={String(term)}
            disabled={disabled}
            options={[3, 6, 12, 24].map((value) => ({
              value: String(value),
              label: `${value} 期`,
            }))}
            onChange={(value) => onTerm(Number(value))}
          />
          <p className="muted">
            仅创建订单，请随后在 Apple
            官网订单页手动付款。本项目不获取分期付款二维码。
            期数和资格以官网为准，不支持时会停止建单。
            {method === "alipay_abc_installment" &&
              "农业银行信用卡需在支付宝付款时手动选择。"}
          </p>
        </>
      )}
    </div>
  );
}
