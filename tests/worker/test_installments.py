"""分期建单的离线契约与网络边界验证；不调用 Apple、银行或支付宝。"""
from copy import deepcopy
from pathlib import Path
from unittest.mock import Mock, patch
import contextlib
import io
import json
import tempfile
import unittest

import requests
import duo_killer as shop
import interaction
import payment_options as payments
import preorder
from protocol import Channel
from service import Service
import test_duo_killer as purchase_tests
import test_preorder as plan_tests


def billing_model(method, lookup=False):
    bank = {'value': 'server-cmb' if method == 'cmb_installment' else 'server-alipay',
            'label': '招商银行' if method == 'cmb_installment' else '支付宝信用卡分期',
            'moduleKey': 'installments', 'controlType': 'RADIO'}
    model = {'d': {'options': [bank]}}
    if lookup:
        model = {'d': {'options': [{'moduleKey': 'bankLookUp', 'controlType': 'TYPEAHEAD',
                  'fieldName': 'selectBank', 'optionsKey': 'banks', 'actionName': 'chooseBank'}]},
                 'bankLookUp': {'d': {'banks': [bank]}}}
    return model


def review_model(method, term):
    return {'installmentsReview': {'d': {
        'paymentType': 'CMB' if method == 'cmb_installment' else 'ALIPAY',
        'bankName': '招商银行' if method == 'cmb_installment' else '农业银行',
        'creditInstallmentTerm': str(term)}}}


def captured_cmb_review():
    """加载 2026-10-13 官网复核页的脱敏付款字段，不含账号或订单信息。"""
    path = Path(__file__).parent / 'fixtures' / 'cmb-24-review.json'
    return json.loads(path.read_text(encoding='utf-8'))['billingReview']


def captured_abc_review():
    """加载官网将农业银行和支付宝渠道分开返回的脱敏复核样本。"""
    path = Path(__file__).parent / 'fixtures' / 'abc-24-review.json'
    return json.loads(path.read_text(encoding='utf-8'))['billingReview']


class BillingFlow:
    """只替换 HTTP 边界；其余建单、复核、结果解析使用正式代码。"""
    def __init__(self, method, lookup=False):
        self.model = purchase_tests.PurchaseFlowTests.review_model()
        self.model['checkout']['billing']['billingOptions'] = billing_model(method, lookup)
        self.session = Mock()
        self.policy = None
        self.headers = {'x-aos-stk': 'fixture'}
        self.url = shop.BASE_URL + '/shop/checkout'
        self.method = method
        self.calls = []
        self.terms = [{'value': term} for term in (3, 6, 12, 24)]
        self.review_override = None

    def page(self, *args):
        pass

    def require(self, path):
        result = shop.field(self.model, path)
        if not isinstance(result, dict):
            raise ValueError('missing ' + path)
        return result

    def action(self, module, action, data=None):
        self.calls.append((module, action, data))
        if action in ('selectBillingOptionAction', 'chooseBank'):
            billing = self.require(payments.BILLING)
            billing['d']['selectBillingOption'] = 'server-installments'
            billing['selectedBillingOptions'] = {'installments': {'d': {},
                'installmentOptions': {'d': {'options': self.terms}}}}
        elif action == 'continueFromBillingToReview':
            self.model['checkout']['review']['billingReview'] = (
                self.review_override or review_model(self.method, data[payments.TERMS + '.selectInstallmentOption']))
        elif action == 'continueFromReviewToProcess':
            self.url = shop.BASE_URL + '/shop/checkout/interstitial'
            self.model = {'externalPayAuth': {'d': {
                'orderNumber': 'W123456789', 'amountDue': 'RMB 3,107',
                'isPost': True, 'externalUrl': 'https://bank.invalid/pay', 'postData': {'private': 'ignored'}}}}


class InstallmentTests(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.stack.enter_context(patch.object(shop, 'ROOT', self.root))
        self.stack.enter_context(patch.object(shop, 'PAYMENT_ROOT', self.root))
        self.stack.enter_context(patch.object(interaction, 'handler', None))
        self.stack.enter_context(patch.object(requests.Session, 'send', side_effect=AssertionError('禁止真实网络')))
        for name in ('reauthenticate_checkout', 'checkout_delivery', 'checkout_shipping', 'save_auth'):
            self.stack.enter_context(patch.object(shop, name))
        self.qr = self.stack.enter_context(patch.object(shop, 'get_payment_qr', side_effect=AssertionError('禁止访问支付平台')))
        self.stack.enter_context(contextlib.redirect_stdout(io.StringIO()))

    def test_both_methods_create_one_order_without_payment_links_or_qr(self):
        for method in ('cmb_installment', 'alipay_abc_installment'):
            for lookup in (False, True):
                with self.subTest(method=method, lookup=lookup), patch('builtins.input', return_value=''):
                    flow = BillingFlow(method, lookup)
                    result = shop.pay_bag(flow=flow, payment_method=method, installment_term=12)
                    self.assertEqual(result['status'], 'pending_payment')
                    self.assertEqual(result['payment_method'], method)
                    self.assertEqual(result['installment_term'], 12)
                    self.assertTrue(result['manual_payment'])
                    self.assertNotIn('payment_url', result)
                    self.assertNotIn('qr_image', result)
                    self.assertNotIn('postData', result)
                    self.assertEqual(sum(c[1] == 'continueFromReviewToProcess' for c in flow.calls), 1)
                    self.assertEqual(json.loads((self.root / 'payment.json').read_text('utf-8')), result)
                    review_data = next(c[2] for c in flow.calls if c[1] == 'continueFromBillingToReview')
                    self.assertEqual(review_data[payments.TERMS + '.selectInstallmentOption'], '12')
                    with self.assertRaisesRegex(ValueError, '不获取付款二维码'):
                        shop.payment_qr(result)
        self.qr.assert_not_called()

    def test_missing_disabled_or_ambiguous_provider_never_submits(self):
        for change in ('missing', 'disabled', 'ambiguous', 'one-time'):
            flow = BillingFlow('cmb_installment')
            options = flow.model['checkout']['billing']['billingOptions']['d']['options']
            if change == 'missing':
                options.clear()
            elif change == 'disabled':
                options[0]['disabled'] = True
            elif change == 'ambiguous':
                options.append(deepcopy(options[0]))
            else:
                options[0]['moduleKey'] = 'creditCard'
            with self.subTest(change=change), self.assertRaisesRegex(ValueError, '不可用或无法唯一识别'):
                shop.pay_bag(flow=flow, payment_method='cmb_installment', installment_term=24)
            self.assertFalse(any(c[1] == 'continueFromReviewToProcess' for c in flow.calls))

    def test_captured_cmb_review_creates_one_manual_payment_order(self):
        flow = BillingFlow('cmb_installment')
        flow.review_override = captured_cmb_review()
        with patch('builtins.input', return_value=''):
            result = shop.pay_bag(flow=flow, payment_method='cmb_installment', installment_term=24)
        self.assertEqual(result['status'], 'pending_payment')
        self.assertEqual(result['payment_method'], 'cmb_installment')
        self.assertEqual(result['installment_term'], 24)
        self.assertTrue(result['manual_payment'])
        self.assertEqual(sum(c[1] == 'continueFromReviewToProcess' for c in flow.calls), 1)
        self.qr.assert_not_called()

    def test_captured_review_invalid_or_ambiguous_never_confirms_or_submits(self):
        changes = [
            {'labelImageAlt': name} for name in ('农业银行', '支付宝', '花呗', '', None)
        ] + [
            {'description': text} for text in (
                '12 个月，每月约 RMB 534', '124 个月，每月约 RMB 534',
                '24.0 个月，每月约 RMB 534', '24 个月或 12 个月，每月约 RMB 534',
                '每月约 RMB 24', '24 个月，信息未知', '', None)
        ] + [
            {'installment': flag} for flag in (False, None, 'true', 1)
        ] + [{'creditInstallmentTerm': '12'}, {'bankName': '农业银行'}, {'paymentType': 'ALIPAY'}]
        cases = []
        for change in changes:
            billing = captured_cmb_review()
            billing['installmentReview']['d'].update(change)
            cases.append((str(change), billing))
        for key in ('labelImageAlt', 'description', 'installment'):
            billing = captured_cmb_review()
            billing['installmentReview']['d'].pop(key)
            cases.append(('missing-' + key, billing))
        for extra in ({'paymentType': 'CREDIT_CARD'}, {'alipayLabel': 'ALIPAY'},
                      {'labelImageAlt': '招商银行', 'installment': True}):
            billing = captured_cmb_review()
            billing['anotherPayment'] = {'d': extra}
            cases.append(('mixed-' + str(extra), billing))
        billing = captured_cmb_review()
        billing.update(review_model('cmb_installment', 24))
        cases.append(('duplicate-review-formats', billing))
        for name, billing in cases:
            flow = BillingFlow('cmb_installment')
            flow.review_override = billing
            with self.subTest(case=name), patch('builtins.input') as confirm, self.assertRaises(ValueError):
                shop.pay_bag(flow=flow, payment_method='cmb_installment', installment_term=24)
            confirm.assert_not_called()
            self.assertFalse(any(c[1] == 'continueFromReviewToProcess' for c in flow.calls))
        self.qr.assert_not_called()

    def test_captured_abc_review_creates_one_manual_payment_order(self):
        flow = BillingFlow('alipay_abc_installment', lookup=True)
        flow.require(payments.BILLING)['bankLookUp']['d']['banks'][0]['label'] = '中国农业银行'
        flow.review_override = captured_abc_review()
        with patch('builtins.input', return_value=''):
            result = shop.pay_bag(flow=flow, payment_method='alipay_abc_installment', installment_term=24)
        self.assertEqual(result['status'], 'pending_payment')
        self.assertEqual(result['payment_method'], 'alipay_abc_installment')
        self.assertEqual(result['installment_term'], 24)
        self.assertTrue(result['manual_payment'])
        self.assertEqual(sum(c[1] == 'continueFromReviewToProcess' for c in flow.calls), 1)
        self.assertNotIn('payment_url', result)
        self.qr.assert_not_called()

    def test_abc_review_requires_matching_bank_channel_and_term_before_submit(self):
        changes = [
            {'paymentProviderLabel': label} for label in ('通过微信支付', '通过支付宝花呗', '', None, True)
        ] + [
            {'labelImageAlt': label} for label in ('招商银行', '中国银行', '花呗', '', None)
        ] + [
            {'description': '12 个月，每月约 RMB 534'}, {'description': None},
            {'installment': False}, {'bankName': '中国银行'}, {'creditInstallmentTerm': '12'},
            {'paymentType': 'WECHAT'},
        ]
        cases = []
        for change in changes:
            billing = captured_abc_review()
            billing['installmentReview']['d'].update(change)
            cases.append((str(change), billing))
        for key in ('labelImageAlt', 'paymentProviderLabel', 'description', 'installment'):
            billing = captured_abc_review()
            billing['installmentReview']['d'].pop(key)
            cases.append(('missing-' + key, billing))
        for extra in ({'paymentType': 'CREDIT_CARD'}, {'paymentProviderLabel': '通过微信支付'},
                      {'alipayLabel': 'ALIPAY'}):
            billing = captured_abc_review()
            billing['anotherPayment'] = {'d': extra}
            cases.append(('mixed-' + str(extra), billing))
        for name, billing in cases:
            flow = BillingFlow('alipay_abc_installment', lookup=True)
            flow.review_override = billing
            with self.subTest(case=name), patch('builtins.input') as confirm, self.assertRaises(ValueError):
                shop.pay_bag(flow=flow, payment_method='alipay_abc_installment', installment_term=24)
            confirm.assert_not_called()
            self.assertFalse(any(c[1] == 'continueFromReviewToProcess' for c in flow.calls))
        self.qr.assert_not_called()

    def test_cmb_review_rejects_conflicting_separate_payment_channel(self):
        billing = captured_cmb_review()
        billing['installmentReview']['d']['paymentProviderLabel'] = '通过支付宝'
        with self.assertRaises(ValueError):
            payments.verify_review({'billingReview': billing}, payments.validate('cmb_installment', 24))

    def test_localized_review_terms_and_alipay_require_explicit_installments(self):
        for method, label in (('cmb_installment', '招商银行'),
                              ('alipay_abc_installment', '支付宝信用卡分期')):
            for term in (3, 6, 12, 24):
                billing = captured_cmb_review()
                billing['installmentReview']['d'].update(
                    labelImageAlt=label, description=f'{term} 个月，每月约 RMB 1,234.56')
                with self.subTest(method=method, term=term):
                    payments.verify_review({'billingReview': billing}, payments.validate(method, term))
        for label in ('招商银行', '农业银行', '支付宝花呗'):
            billing = captured_cmb_review()
            billing['installmentReview']['d']['labelImageAlt'] = label
            with self.subTest(label=label), self.assertRaises(ValueError):
                payments.verify_review({'billingReview': billing}, payments.validate('alipay_abc_installment', 24))

    def test_unavailable_term_and_wrong_review_never_confirm_or_submit(self):
        for case in ('missing-term', 'disabled-term', 'wrong-bank', 'wrong-term', 'one-time', 'huabei', 'mixed'):
            flow = BillingFlow('alipay_abc_installment')
            if case == 'missing-term':
                flow.terms = [{'value': 6}]
            elif case == 'disabled-term':
                flow.terms = [{'value': 24, 'disabled': True}]
            else:
                data = review_model('alipay_abc_installment', 24)
                row = data['installmentsReview']['d']
                if case == 'wrong-bank': row['bankName'] = '中国银行'
                if case == 'wrong-term': row['creditInstallmentTerm'] = '12'
                if case == 'one-time': row.pop('creditInstallmentTerm')
                if case == 'huabei': row.update(bankName='花呗', paymentType='ALIPAY_HUABEI')
                if case == 'mixed': data['anotherPayment'] = {'d': {'paymentType': 'CREDIT_CARD'}}
                flow.review_override = data
            with self.subTest(case=case), patch('builtins.input') as confirm, self.assertRaises(ValueError):
                shop.pay_bag(flow=flow, payment_method='alipay_abc_installment', installment_term=24)
            confirm.assert_not_called()
            self.assertFalse(any(c[1] == 'continueFromReviewToProcess' for c in flow.calls))

    def test_invalid_parameters_fail_before_auth_or_bag_changes(self):
        for method, term in [('other', 24), ('cmb_installment', None), ('cmb_installment', True),
                             ('cmb_installment', '24'), ('cmb_installment', 36), ('alipay', 24)]:
            with self.subTest(method=method, term=term), patch.object(shop, 'ensure_auth_file') as auth:
                with self.assertRaises(ValueError):
                    shop.purchase_sku('MJY64CH', payment_method=method, installment_term=term)
                auth.assert_not_called()

    def test_result_identity_and_amount_required_without_following_redirect(self):
        payment = payments.validate('cmb_installment', 24)
        for header in ({'amountDue': 'RMB 1'}, {'orderNumber': 'W123456789'},
                       {'orderNumber': 'invalid', 'amountDue': 'RMB 1'}):
            with self.assertRaises(ValueError):
                shop.payment_result(Mock(model={'thankYouInterstitial': {'d': header}}), payment)
        self.qr.assert_not_called()

    def test_cancel_and_submit_timeout_do_not_retry_or_fetch_qr(self):
        flow = BillingFlow('cmb_installment')
        with patch('builtins.input', return_value='n'), self.assertRaisesRegex(ValueError, '已取消'):
            shop.pay_bag(flow=flow, payment_method='cmb_installment', installment_term=24)
        self.assertFalse(any(c[1] == 'continueFromReviewToProcess' for c in flow.calls))
        flow = BillingFlow('cmb_installment')
        original_action = flow.action
        def timeout(module, name, data=None):
            original_action(module, name, data)
            if name == 'continueFromReviewToProcess':
                raise requests.Timeout('synthetic')
        flow.action = timeout
        with patch('builtins.input', return_value=''), patch.object(interaction, 'report_stage') as stage:
            with self.assertRaises(requests.Timeout):
                shop.pay_bag(flow=flow, payment_method='cmb_installment', installment_term=24)
            stage.assert_called_with('正在提交订单', order_submitted=True)
        self.assertEqual(sum(c[1] == 'continueFromReviewToProcess' for c in flow.calls), 1)
        self.qr.assert_not_called()

    def test_service_forwards_selection_and_rejects_qr_for_saved_manual_order(self):
        service = Service(Channel('a', 't', io.StringIO()), self.root, self.root, 'test@example.invalid', Mock())
        payment = payments.validate('cmb_installment', 6)
        with patch.object(service, 'ensure_auth_file'), patch.object(service, 'persist_password'), \
                patch.object(shop, 'purchase_sku', return_value={'success': True}) as buy, \
                patch.object(shop, 'pay_bag', return_value={'success': True}) as bag:
            service.run('purchase', {'sku': 'MJY64CH', 'quantity': 1, **payment})
            buy.assert_called_once_with('MJY64CH', quantity=1, applecare=False, **payment)
            service.run('checkout', payment)
            bag.assert_called_once_with(**payment)
            source = '00000000-0000-0000-0000-000000000001'
            directory = self.root / 'tasks' / source
            directory.mkdir(parents=True)
            shop.write_private_json(directory / 'payment.json', {'manual_payment': True, **payment})
            with self.assertRaisesRegex(ValueError, '不获取付款二维码'):
                service.run('qr', {'source_task': source})
        self.qr.assert_not_called()

    def test_plan_defaults_and_consent_match(self):
        self.assertEqual(preorder.validate_plan(plan_tests.params())['payment_method'], 'alipay')
        plan = preorder.validate_plan(plan_tests.params(payment_method='cmb_installment', installment_term=24))
        self.assertEqual(plan['installment_term'], 24)
        store = Mock(data={'plan': plan})
        policy = preorder.Policy(Mock(), store, self.root / 'checkout.json')
        with self.assertRaisesRegex(ValueError, '付款方式或期数'):
            policy.confirm({'payment_method': 'alipay'})


class InstallmentPreorderTests(unittest.TestCase):
    # 复用已有计划测试的临时账号和网络隔离，不继承其测试用例。
    setUp = plan_tests.PreorderTests.setUp

    def make_flow(self, session, policy):
        flow = plan_tests.PurchaseFlow(session, policy)
        original_action = flow.action
        method = policy.plan.get('payment_method', 'alipay')
        if method == 'alipay':
            self.flow = flow
            return flow

        def action(module, name, data=None):
            original_action(module, name, data)
            billing = shop.field(flow.model, payments.BILLING)
            if billing is not None:
                billing.update(billing_model(method))
            if name == 'selectBillingOptionAction':
                billing['selectedBillingOptions'] = {'installments': {
                    'installmentOptions': {'d': {'options': [{'value': 24}]}}}}
            if name == 'continueFromBillingToReview':
                flow.model['checkout']['review']['billingReview'] = (
                    captured_cmb_review() if method == 'cmb_installment' else captured_abc_review())
            if name == 'continueFromReviewToProcess':
                flow.model['externalPayAuth']['d'].pop('externalUrl')
        flow.action = action
        self.flow = flow
        return flow

    def test_saved_installment_resumes_without_changing_selection_or_resubmitting(self):
        result = self.service.run('preorder', plan_tests.params(payment_method='alipay_abc_installment', installment_term=24))
        self.assertEqual(result['status'], 'pending_payment')
        self.assertEqual(result['payment_method'], 'alipay_abc_installment')
        self.assertEqual(result['installment_term'], 24)
        self.assertTrue(result['manual_payment'])
        self.assertEqual(self.flow.submissions, 1)
        self.assertEqual(preorder.PlanStore(self.root).data['plan']['installment_term'], 24)
        self.assertEqual(self.service.run('preorder_resume', {'payment_method': 'cmb_installment', 'installment_term': 6}), result)
        self.assertEqual(self.flow.submissions, 1)
        self.qr.assert_not_called()

    def test_legacy_saved_plan_defaults_to_alipay_before_resuming(self):
        plan = preorder.validate_plan(plan_tests.params())
        plan.pop('payment_method')
        plan.pop('installment_term')
        store = preorder.PlanStore(self.root)
        store.data = {'version': 1, 'plan': plan, 'address': deepcopy(plan_tests.CONFIG)}
        store.save(state='waiting')
        result = self.service.run('preorder_resume', {})
        self.assertEqual(result['status'], 'pending_payment')
        self.assertNotIn('manual_payment', result)
        self.assertEqual(self.flow.submissions, 1)
        self.qr.assert_not_called()

    def test_captured_cmb_review_preorder_submits_once_and_resumes_same_order(self):
        result = self.service.run('preorder', plan_tests.params(
            payment_method='cmb_installment', installment_term=24))
        self.assertEqual(result['status'], 'pending_payment')
        self.assertEqual(result['payment_method'], 'cmb_installment')
        self.assertEqual(result['installment_term'], 24)
        self.assertTrue(result['manual_payment'])
        self.assertEqual(self.flow.submissions, 1)
        self.assertEqual(preorder.PlanStore(self.root).data['plan']['quantity'], 1)
        self.assertEqual(self.service.run('preorder_resume', {}), result)
        self.assertEqual(self.factory.call_count, 1)
        self.qr.assert_not_called()
