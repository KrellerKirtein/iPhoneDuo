"""按官网当前结账模型选择分期；仅建单，不访问银行或支付宝付款入口。"""
import re

from checkout_options import value


LABELS = {'alipay': '支付宝', 'cmb_installment': '招商银行分期',
          'alipay_abc_installment': '支付宝·农业银行分期'}
BILLING = 'checkout.billing.billingOptions'
INSTALLMENTS = BILLING + '.selectedBillingOptions.installments'
TERMS = INSTALLMENTS + '.installmentOptions'


def validate(method='alipay', term=None):
    """拒绝未知方式和含糊期数，旧调用缺省为原有支付宝。"""
    if not isinstance(method, str) or method not in LABELS:
        raise ValueError('不支持的付款方式。')
    if method == 'alipay':
        if term is not None:
            raise ValueError('普通支付宝付款不能指定分期期数。')
    elif type(term) is not int or term not in (3, 6, 12, 24):
        raise ValueError('请选择 3、6、12 或 24 期；实际可用期数以官网为准。')
    return {'payment_method': method, 'installment_term': term}


def enabled(option):
    """仅接受未被官网禁用的可选项。"""
    return (isinstance(option, dict) and not option.get('disabled')
            and not option.get('disabledMessage') and option.get('enabled') is not False)


def provider_matches(option, method):
    """通过页面返回的机构名称或明确代码识别，不生成银行编号。"""
    text = ' '.join(str(option.get(key, '')) for key in
                    ('value', 'label', 'labelImageAlt', 'bankNameLabel', 'bankName', 'paymentType')).upper()
    cmb = '招商' in text or bool(re.search(r'(?<![A-Z])CMB(?:CHINA)?(?![A-Z])|CHINA MERCHANTS', text))
    abc = '农业' in text or bool(re.search(r'(?<![A-Z])ABC(?![A-Z])|AGRICULTURAL BANK', text))
    alipay = 'ALIPAY' in text or '支付宝' in text
    if method == 'cmb_installment':
        return cmb and not alipay
    return not cmb and '花呗' not in text and 'HUABEI' not in text and (abc or alipay)


def identifier(text):
    """页面元数据只能指向当前 billing 模块下的字段或动作。"""
    if not isinstance(text, str) or not re.fullmatch(r'[A-Za-z][A-Za-z0-9]*', text):
        raise ValueError('官网分期选项元数据无法识别，未提交订单。')
    return text


def select_billing(flow, payment):
    """选择唯一机构和用户指定期数，使用页面下发的 action、字段和值。"""
    method, term = payment['payment_method'], payment['installment_term']
    options = value(flow.require(BILLING), 'd.options', [])
    data = {BILLING + '.bankLookUp.selectBank': ''}
    if method == 'alipay':
        if not any(enabled(o) and o.get('value') == 'ALIPAY' for o in options):
            raise ValueError('当前订单不支持支付宝。')
        data[BILLING + '.selectBillingOption'] = 'ALIPAY'
        flow.action(BILLING, 'selectBillingOptionAction',
                    {**data, 'checkout.locationConsent.locationConsent': 'false'})
    else:
        candidates = []
        for option in options:
            if not enabled(option):
                continue
            if option.get('moduleKey') == 'installments' and provider_matches(option, method):
                candidates.append((option, None))
            if option.get('controlType') == 'TYPEAHEAD' and option.get('moduleKey') == 'bankLookUp':
                key = identifier(option.get('optionsKey'))
                for bank in value(flow.model, BILLING + '.bankLookUp.d.' + key, []):
                    if enabled(bank) and provider_matches(bank, method):
                        candidates.append((bank, option))
        if len(candidates) != 1:
            raise ValueError(LABELS[method] + '不可用或无法唯一识别，未提交订单。')
        bank, lookup = candidates[0]
        if not isinstance(bank.get('value'), str) or not bank['value']:
            raise ValueError('官网未返回有效分期机构值。')
        if lookup:
            field = identifier(lookup.get('fieldName'))
            action = identifier(lookup.get('actionName'))
            data[BILLING + '.bankLookUp.' + field] = bank['value']
            flow.action(BILLING + '.bankLookUp', action,
                        {**data, 'checkout.locationConsent.locationConsent': 'false'})
            selected = value(flow.model, BILLING + '.d.selectBillingOption')
            if not isinstance(selected, str) or not selected or selected == 'ALIPAY':
                raise ValueError('官网未确认分期付款通道，未提交订单。')
            data[BILLING + '.selectBillingOption'] = selected
        else:
            data[BILLING + '.selectBillingOption'] = bank['value']
            flow.action(BILLING, 'selectBillingOptionAction',
                        {**data, 'checkout.locationConsent.locationConsent': 'false'})
        installments = flow.require(INSTALLMENTS)
        terms = flow.require(TERMS)
        if value(installments, 'd.disableIssuerMessage') or value(terms, 'd.disableIssuerMessage'):
            raise ValueError(LABELS[method] + '当前不可用，未提交订单。')
        matches = [o for o in value(terms, 'd.options', [])
                   if enabled(o) and str(o.get('value')) == str(term)]
        if len(matches) != 1:
            raise ValueError(f'当前订单不支持所选 {term} 期分期，未提交订单。')
        data[TERMS + '.selectInstallmentOption'] = str(matches[0]['value'])
    flow.action('checkout.billing', 'continueFromBillingToReview', data)


def review_nodes(node):
    """仅遍历复核模块的数据节点，不使用请求回显作为付款方式证据。"""
    if isinstance(node, dict):
        if isinstance(node.get('d'), dict):
            yield node['d']
        for key, child in node.items():
            if key not in ('d', 'a', 'b'):
                yield from review_nodes(child)
    elif isinstance(node, list):
        for child in node:
            yield from review_nodes(child)


def localized_installment_matches(data, payment):
    """核对官网中文分期复核卡片；只接受明确的机构、分期标记及月数描述。"""
    label, description = data.get('labelImageAlt'), data.get('description')
    if (data.get('installment') is not True or not isinstance(label, str)
            or not isinstance(description, str)):
        return False
    method = payment['payment_method']
    if not provider_matches({'labelImageAlt': label}, method):
        return False
    # 官网可将银行名称和付款渠道分开返回；说明文字不能代替明确的渠道字段。
    provider = data.get('paymentProviderLabel', label)
    if not isinstance(provider, str) or not provider_matches({'label': provider}, method):
        return False
    if method == 'alipay_abc_installment' and not ('支付宝' in provider or 'ALIPAY' in provider.upper()):
        return False
    # 官网将月数放在展示文案中；完整匹配已确认格式，不能将月供金额当成期数。
    match = re.fullmatch(r'\s*([0-9]+)\s*个月[，,]\s*每月约\s+RMB\s+'
                         r'[0-9]+(?:,[0-9]{3})*(?:\.[0-9]{1,2})?\s*', description)
    if not match or match[1] != str(payment['installment_term']):
        return False
    # 若页面同时给出结构化字段，也必须一致，不能以展示文案掩盖冲突。
    if ('creditInstallmentTerm' in data
            and str(data['creditInstallmentTerm']) != str(payment['installment_term'])):
        return False
    return all(provider_matches({key: data[key]}, method)
               for key in ('paymentType', 'bankName') if key in data)


def verify_review(review, payment):
    """提交前核对官网复核页的付款机构和期数，缺失或不一致即停止。"""
    method = payment['payment_method']
    if method == 'alipay':
        valid = value(review, 'billingReview.alipayReview.d.alipayLabel') == 'ALIPAY'
    else:
        valid = False
        billing = review.get('billingReview', {})
        localized = value(billing, 'installmentReview.d')
        rows = [data for data in review_nodes(billing)
                if data is localized or any(key in data for key in (
                    'paymentType', 'bankName', 'creditInstallmentTerm', 'installment',
                    'alipayLabel', 'paymentProviderLabel'))]
        # 分期只支持单一机构；混合支付或多条机构信息不能取其中一条当作整单证据。
        for data in rows if len(rows) == 1 else []:
            if isinstance(billing, dict) and 'installmentReview' in billing:
                valid = data is localized and localized_installment_matches(data, payment)
                break
            if str(data.get('creditInstallmentTerm')) != str(payment['installment_term']):
                continue
            if not provider_matches(data, method):
                continue
            if method == 'alipay_abc_installment':
                # 农业银行卡在支付宝手动选择，但必须先确认为支付宝分期订单。
                provider = str(data.get('paymentType', '')).upper()
                if 'ALIPAY' not in provider:
                    continue
                bank = data.get('bankName')
                if bank and not provider_matches({'bankName': bank}, method):
                    continue
            valid = True
            break
    if not valid:
        raise ValueError('订单复核付款机构或分期期数与选择不一致，未提交订单。')


def description(payment):
    """生成用于人工确认、计划与结果展示的付款说明。"""
    method = payment['payment_method']
    return LABELS[method] + (f" · {payment['installment_term']} 期 · 手动付款" if method != 'alipay' else '')
