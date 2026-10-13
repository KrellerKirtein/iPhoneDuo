"""离线故障注入：真实计划、持久化和结账控制流，商城响应使用合成模型。"""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
import contextlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
import uuid
from unittest.mock import Mock, patch

import requests
import duo_killer as shop
import interaction
import preorder
from protocol import Channel, Cancelled
from service import Service
from test_checkout_options import CONFIG, FakeFlow, fulfillment_model


SKU = 'MK2U4CH/A'
ITEM = {'d': {'sku': SKU, 'name': '测试手机 1TB 黑色', 'totalPrice': 'RMB 21,499'},
        'itemQuantity': {'d': {'quantity': 1}}}
REVIEW = {'items': [{'sku': SKU, 'name': ITEM['d']['name'], 'quantity': 1}],
          'total': 'RMB 21,499', 'address': CONFIG['address']['street'], 'store': ''}
ORDER = {'order_number': 'W123456789', 'items': REVIEW['items']}
EMPTY = {'success': True, 'has_more': False, 'orders': []}
PAYMENT = {'success': True, 'status': 'pending_payment', 'order_number': ORDER['order_number'],
           'amount_due': REVIEW['total'], 'payment_url': 'https://openapi.alipay.com/gateway.do?test=synthetic'}


def params(**changes):
    return {**{'authorized': True, 'sku': SKU, 'quantity': 1,
               'target_at': (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),
               'window_seconds': 300, 'max_total': '21499', 'pickup_stores': [], 'id_suffix': ''}, **changes}


class PurchaseFlow(FakeFlow):
    """跑真实 pay_bag/fulfillment/shipping，只有 HTTP 边界替换为模型转移。"""
    def __init__(self, session, policy):
        super().__init__({'shoppingCart': {'items': {'item-phone': deepcopy(ITEM)},
                         'summary': {'d': {'total': REVIEW['total']}},
                         'actions': {'a': {'checkout': {'url': '/synthetic'}}}}}, mixed=False)
        self.session, self.policy = session, policy
        self.headers = {'x-aos-stk': 'synthetic'}
        self.page_response = object()
        self.submissions = 0
        self.failure = None

    def page(self, url, stage):
        self.policy.before_action()

    def action(self, module, name, data=None):
        self.policy.before_action()
        if name == 'checkout':
            self.model = fulfillment_model(mixed=False)
        elif name == 'selectBillingOptionAction':
            pass
        elif name == 'continueFromBillingToReview':
            self.model = {'checkout': {'review': {
                'fulfillmentReview': {'items': {'item-phone': deepcopy(ITEM)}},
                'summary': {'d': {'total': REVIEW['total']}},
                'placeOrder': {'d': {'enableContinue': True}},
                'billingReview': {'alipayReview': {'d': {'alipayLabel': 'ALIPAY'}}}},
                'companionBar': {'orderDetails': {'shippingCompanionBar': {
                    'address': {'d': {'street': CONFIG['address']['street']}}}}}}}
        elif name == 'continueFromReviewToProcess':
            # 断言发送任何创建请求之前，磁盘上已存在完整提交意图。
            saved = preorder.PlanStore(self.policy.service.root).data
            assert saved['state'] == 'submitting' and saved['review']['items'][0]['sku'] == SKU
            self.submissions += 1
            if self.failure:
                raise self.failure
            self.url = shop.BASE_URL + '/shop/checkout/interstitial'
            self.model = {'externalPayAuth': {'d': {'externalUrl': PAYMENT['payment_url'],
                          'orderNumber': PAYMENT['order_number'], 'amountDue': PAYMENT['amount_due']}}}
        else:
            super().action(module, name, data)
            if shop.field(self.model, 'checkout.billing') is not None:
                self.model['checkout']['billing']['billingOptions'] = {'d': {'options': [{'value': 'ALIPAY'}]}}


class PreorderTests(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.task = self.root / 'task'
        self.task.mkdir()
        self.channel = Channel('account', 'task', io.StringIO())
        self.stack.enter_context(patch.object(self.channel.cancelled, 'wait', return_value=False))
        for name in ('ROOT', 'AUTH_PATH', 'LOG_PATH', 'PAYMENT_ROOT'):
            self.stack.enter_context(patch.object(shop, name, getattr(shop, name)))
        self.stack.enter_context(patch.object(interaction, 'handler', None))
        self.service = Service(self.channel, self.root, self.task, 'test@example.invalid', Mock())
        shop.write_private_json(self.service.checkout, deepcopy(CONFIG))
        self.stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
        self.stack.enter_context(patch.object(requests.Session, 'send', side_effect=AssertionError('禁止真实网络')))
        for name in ('load_auth', 'save_auth', 'ensure_strong_login'):
            self.stack.enter_context(patch.object(shop, name))
        self.stack.enter_context(patch.object(shop, 'check_login', return_value={'logged_in': True}))
        self.orders = self.stack.enter_context(patch.object(shop, 'order_list', return_value=deepcopy(EMPTY)))
        self.qr = self.stack.enter_context(patch.object(shop, 'payment_qr', side_effect=AssertionError('自动流程不能访问支付平台')))
        self.flow = None
        self.failure = None
        self.factory = self.stack.enter_context(patch.object(preorder, 'PlanCheckout', side_effect=self.make_flow))

    def make_flow(self, session, policy):
        self.flow = PurchaseFlow(session, policy)
        self.flow.failure = self.failure
        return self.flow

    def policy(self, **changes):
        store = preorder.PlanStore(self.root)
        store.data = {'version': 1, 'plan': preorder.validate_plan(params(**changes)), 'address': deepcopy(CONFIG)}
        store.save(state='running', baseline=[], baseline_complete=True)
        policy = preorder.Policy(self.service, store, self.service.checkout)
        self.service.preorder_policy = policy
        return policy

    def test_complete_order_once_and_stop_before_payment(self):
        result = self.service.run('preorder', params())
        self.assertEqual(result['status'], 'pending_payment')
        self.assertEqual(self.flow.submissions, 1)
        self.assertEqual(preorder.PlanStore(self.root).data['state'], 'confirmed')
        self.assertEqual(json.loads((self.task / 'payment.json').read_text('utf-8')), result)
        self.qr.assert_not_called()
        # 重复启动和恢复均返回原单，不重新读袋/提交。
        self.assertEqual(self.service.run('preorder', params()), result)
        self.assertEqual(self.service.run('preorder_resume', {}), result)
        self.assertEqual(self.factory.call_count, 1)

    def test_accepted_but_response_timed_out_recovers_read_only(self):
        self.failure = requests.Timeout('synthetic accepted response lost')
        self.orders.side_effect = [deepcopy(EMPTY), deepcopy(EMPTY), {**EMPTY, 'orders': [ORDER]}]
        result = self.service.run('preorder', params())
        self.assertEqual(result['status'], 'order_confirmed')
        self.assertEqual(result['payment_status'], 'unknown')
        self.assertEqual(self.flow.submissions, 1)
        self.assertEqual(self.factory.call_count, 1)
        self.qr.assert_not_called()

    def test_invisible_order_stays_sealed_after_restart_and_manual_purchase_blocked(self):
        self.failure = requests.Timeout()
        self.assertEqual(self.service.run('preorder', params())['status'], 'order_uncertain')
        self.assertEqual(preorder.PlanStore(self.root).data['state'], 'uncertain')
        restarted = Service(self.channel, self.root, self.task, 'test@example.invalid', Mock())
        for action in ('preorder_resume', 'preorder'):
            self.assertEqual(restarted.run(action, params())['status'], 'order_uncertain')
        for action in ('purchase', 'checkout', 'add'):
            with self.assertRaisesRegex(ValueError, '阻止再次购买'):
                restarted.run(action, {'sku': SKU})
        self.assertEqual(self.factory.call_count, 1)
        self.orders.return_value = {**EMPTY, 'orders': [ORDER]}
        self.assertEqual(restarted.run('preorder_resume', {})['order_number'], ORDER['order_number'])

    def test_cancel_after_intent_is_durable_and_never_resubmitted(self):
        self.failure = Cancelled()
        with self.assertRaises(Cancelled):
            self.service.run('preorder', params())
        self.assertEqual(preorder.PlanStore(self.root).data['state'], 'submitting')
        self.orders.return_value = {**EMPTY, 'orders': [ORDER]}
        self.assertEqual(self.service.run('preorder_resume', {})['status'], 'order_confirmed')
        self.assertEqual(self.factory.call_count, 1)

    def test_unexpected_parser_failure_after_submit_reconciles(self):
        self.failure = KeyError('changed remote model')
        self.orders.side_effect = [deepcopy(EMPTY), {**EMPTY, 'orders': [ORDER]}]
        self.assertEqual(self.service.run('preorder', params())['status'], 'order_confirmed')
        self.assertEqual(self.flow.submissions, 1)

    def test_baseline_orders_ambiguity_and_incomplete_listing_never_confirm(self):
        policy = self.policy()
        policy.confirm(deepcopy(REVIEW))
        policy.before_submit()
        for listing, baseline in [({**EMPTY, 'orders': [ORDER]}, [ORDER['order_number']]),
                                  ({**EMPTY, 'orders': [ORDER, {**ORDER, 'order_number': 'W987654321'}]}, []),
                                  ({**EMPTY, 'orders': [ORDER], 'has_more': True}, []),
                                  ({**EMPTY, 'orders': [{**ORDER, 'items': []}]}, [])]:
            with self.subTest(listing=listing):
                policy.store.save(baseline=baseline)
                self.orders.return_value = listing
                self.assertEqual(preorder.reconcile(self.service, policy.store, None)['status'], 'order_uncertain')

    def test_expired_plan_never_enters_cart(self):
        result = self.service.run('preorder', params(target_at='2020-10-16T20:00:00+08:00'))
        self.assertEqual(result['state'], 'expired')
        self.factory.assert_not_called()

    def test_historical_pagination_allows_creation_but_not_speculative_recovery(self):
        self.orders.return_value = {**EMPTY, 'has_more': True, 'orders': [{**ORDER, 'order_number': 'W111111111'}]}
        self.assertEqual(self.service.run('preorder', params())['status'], 'pending_payment')
        store = preorder.PlanStore(self.root)
        self.assertFalse(store.data['baseline_complete'])
        store.save(state='submitting')
        self.orders.return_value = {**EMPTY, 'orders': [ORDER]}
        self.assertEqual(preorder.reconcile(self.service, store, None)['status'], 'order_uncertain')

    def test_schedule_preflight_before_target_and_no_early_cart(self):
        target = datetime.now(timezone.utc) + timedelta(days=1)
        waits = []
        def waiting(service, value):
            self.factory.assert_not_called()
            waits.append(value)
            if len(waits) == 2:
                raise Cancelled()
        with patch.object(preorder, 'wait_until', side_effect=waiting), self.assertRaises(Cancelled):
            self.service.run('preorder', params(target_at=target.isoformat()))
        self.assertEqual(waits, [target - timedelta(seconds=30), target])
        self.orders.assert_called_once()
        self.assertEqual(preorder.PlanStore(self.root).data['state'], 'paused')

    def test_absolute_wait_rechecks_clock_and_cancel(self):
        target = datetime.now(timezone.utc)
        now = Mock(side_effect=[target - timedelta(seconds=1), target - timedelta(seconds=.2), target])
        pause = Mock()
        preorder.wait_until(self.service, target, now=now, pause=pause)
        self.assertEqual([call.args[0] for call in pause.call_args_list], [.5, .2])
        self.channel.cancelled.set()
        with self.assertRaises(Cancelled):
            preorder.wait_until(self.service, target)

    def test_intent_write_failure_prevents_order_post(self):
        original = shop.write_private_json
        def write(path, data):
            if path.name == 'preorder.json' and data.get('state') == 'submitting':
                raise OSError('disk unavailable')
            original(path, data)
        with patch.object(shop, 'write_private_json', side_effect=write):
            self.service.run('preorder', params())
        self.assertEqual(self.flow.submissions, 0)

    def test_pre_submit_retry_reads_new_bag(self):
        original = shop.add_to_bag
        calls = []
        def add(*args, **kwargs):
            calls.append(kwargs['flow'])
            if len(calls) == 1:
                raise requests.Timeout('bag read timed out')
            return original(*args, **kwargs)
        with patch.object(shop, 'add_to_bag', side_effect=add):
            self.assertEqual(self.service.run('preorder', params())['status'], 'pending_payment')
        self.assertIsNot(calls[0], calls[1])
        self.assertEqual(self.flow.submissions, 1)

    def test_accepted_add_then_bag_timeout_does_not_add_twice(self):
        created = []
        def factory(session, policy):
            flow = PurchaseFlow(session, policy)
            if not created:
                flow.model['shoppingCart']['items'] = {}
                page_reads = []
                def read(url, stage):
                    page_reads.append(stage)
                    if len(page_reads) == 2:
                        raise requests.Timeout('add accepted but bag read failed')
                flow.page = read
            created.append(flow)
            return flow
        self.factory.side_effect = factory
        with patch.object(shop, '_add_one_to_bag', return_value='') as add:
            self.assertEqual(self.service.run('preorder', params())['status'], 'pending_payment')
        add.assert_called_once()
        self.assertEqual(len(created), 2)
        self.assertEqual(sum(flow.submissions for flow in created), 1)

    def test_cancel_between_intent_write_and_post_keeps_protection(self):
        original = self.service.stage
        def stage(message, **fields):
            if fields.get('order_submitted'):
                self.channel.cancelled.set()
            original(message, **fields)
        with patch.object(self.service, 'stage', side_effect=stage), self.assertRaises(Cancelled):
            self.service.run('preorder', params())
        self.assertEqual(self.flow.submissions, 0)
        self.assertTrue(self.service.submitted)
        self.assertEqual(preorder.PlanStore(self.root).data['state'], 'submitting')

    def test_wrong_returned_amount_never_allows_another_submission(self):
        with patch.object(shop, 'finish_payment', return_value={**PAYMENT, 'amount_due': 'RMB 1'}):
            self.assertEqual(self.service.run('preorder', params())['status'], 'order_uncertain')
        self.assertEqual(self.flow.submissions, 1)
        self.assertEqual(self.factory.call_count, 1)

    def test_invalid_plan_review_or_price_never_submits(self):
        policy = self.policy()
        for changes in ({'total': 'RMB 21500'}, {'total': 'USD 100'}, {'address': '其他地址'},
                        {'items': REVIEW['items'] * 2}, {'items': [{**REVIEW['items'][0], 'quantity': 2}]},
                        {'items': [{**REVIEW['items'][0], 'sku': 'MLH74FE/A'}]}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                policy.confirm({**REVIEW, **changes})
        self.assertIsNone(policy.review)
        for changes in ({'authorized': False}, {'sku': None}, {'quantity': True}, {'quantity': 2},
                        {'max_total': 'NaN'}, {'max_total': '1,,000'}, {'window_seconds': 901},
                        {'target_at': '2026-10-16T20:00:00'}, {'pickup_stores': ['R580'], 'id_suffix': ''}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                preorder.validate_plan(params(**changes))
        self.assertEqual(preorder.parse_time('2026-10-16T20:00:00+08:00').hour, 12)

    def test_shipping_priority_and_snapshot_address(self):
        policy = self.policy(pickup_stores=['R580'], id_suffix='1234')
        flow = FakeFlow(fulfillment_model(mixed=False), mixed=False)
        flow.policy = policy
        policy.fulfillment(flow)
        shop.checkout_shipping(flow)
        self.assertEqual(policy.mode, 'HOME')
        self.assertNotIn('search', [name for _, name, _ in flow.calls])
        payload = flow.calls[-1][2]
        self.assertEqual(payload['checkout.shipping.addressSelector.selectAddress'], 'newAddr')
        self.assertEqual(payload['checkout.shipping.addressSelector.newAddress.saveToAddressBook'], 'false')

    def test_explicit_no_shipping_falls_back_and_store_review_must_match(self):
        policy = self.policy(pickup_stores=['R999', 'R580'], id_suffix='1234')
        model = fulfillment_model(mixed=False)
        path = 'checkout.fulfillment.deliveryTab.delivery.shipmentGroups.group.shipmentOptionsGroups.option.shippingOptions.d'
        shop.field(model, path)['options'] = []
        flow = FakeFlow(model, mixed=False)
        flow.policy = policy
        policy.fulfillment(flow)
        self.assertEqual(policy.store_id, 'R580')
        self.assertEqual(policy.mode, 'RETAIL')
        policy.confirm({**REVIEW, 'store': '测试门店'})
        with self.assertRaises(ValueError):
            policy.confirm({**REVIEW, 'store': '其他门店'})

    def test_unknown_shipping_and_network_failure_do_not_trigger_pickup(self):
        for failure in (ValueError('unknown response'), requests.Timeout()):
            policy = self.policy(pickup_stores=['R580'], id_suffix='1234')
            flow = FakeFlow(fulfillment_model(mixed=False), mixed=False)
            flow.policy = policy
            with patch.object(preorder.options, 'home_delivery', side_effect=failure), self.assertRaises(type(failure)):
                policy.fulfillment(flow)
            self.assertEqual(flow.calls, [])

    def test_payment_requires_order_id_and_amount(self):
        flow = Mock(model={'externalPayAuth': {'d': {'externalUrl': PAYMENT['payment_url'],
                         'orderNumber': PAYMENT['order_number'], 'amountDue': PAYMENT['amount_due']}}})
        self.assertEqual(shop.payment_result(flow)['order_number'], PAYMENT['order_number'])
        for key in ('orderNumber', 'amountDue'):
            copy = deepcopy(flow.model)
            del copy['externalPayAuth']['d'][key]
            with self.subTest(key=key), self.assertRaises(ValueError):
                shop.payment_result(Mock(model=copy))

    def test_pickup_slot_uses_current_server_signature_and_date(self):
        slot = {'checkInStart': '01:00 PM', 'checkInEnd': '01:15 PM', 'SlotId': 'server-id',
                'signKey': 'server-signature', 'timeZone': 'Asia/Shanghai', 'timeSlotValue': '16-13:00-13:15'}
        model = {'checkout': {'fulfillment': {'pickupTab': {'pickup': {'timeSlot': {'dateTimeSlots': {'d': {
            'pickUpDates': [{'date': '2026-10-16'}], 'timeSlotWindows': [{'16': [slot]}]}}}}}}}}
        data = preorder.pickup_slot(model)
        prefix = 'checkout.fulfillment.pickupTab.pickup.timeSlot.dateTimeSlots.'
        self.assertEqual(data[prefix + 'date'], '2026-10-16')
        self.assertEqual(data[prefix + 'signKey'], 'server-signature')
        slot.pop('signKey')
        with self.assertRaisesRegex(ValueError, '签名'):
            preorder.pickup_slot(model)
        slot['disabled'] = True
        with self.assertRaises(preorder.Unavailable):
            preorder.pickup_slot(model)

    def test_corrupt_journal_blocks_manual_and_automatic_purchase(self):
        for value in ('{', '[]', '{}'):
            (self.root / 'preorder.json').write_text(value, encoding='utf-8')
            with self.assertRaises(ValueError):
                self.service.run('preorder', params())
            with self.assertRaises(ValueError):
                self.service.run('purchase', {'sku': SKU})
        self.factory.assert_not_called()

    def test_new_worker_process_restores_confirmed_order_without_authentication(self):
        account, task = str(uuid.uuid4()), str(uuid.uuid4())
        directory = self.root / 'accounts' / account
        directory.mkdir(parents=True)
        store = preorder.PlanStore(directory)
        store.data = {'version': 1, 'plan': preorder.validate_plan(params())}
        store.save(state='confirmed', result=PAYMENT)
        process = subprocess.Popen([sys.executable, str(Path(__file__).resolve().parents[2] / 'worker/main.py'),
                                    '--data-dir', str(self.root), '--account-id', account, '--task-id', task],
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   text=True, encoding='utf-8')
        try:
            process.stdin.write(json.dumps({'account_id': account, 'task_id': task, 'account': 'test@example.invalid',
                                            'action': 'preorder_resume', 'params': {}}) + '\n')
            process.stdin.flush()
            self.assertEqual(process.wait(timeout=15), 0)
            event = json.loads(process.stdout.readline())
            self.assertEqual(event['type'], 'result', event)
            self.assertTrue(event['order_submitted'])
            self.assertEqual(event['result']['order_number'], PAYMENT['order_number'])
            self.assertTrue((directory / 'tasks' / task / 'payment.json').exists())
            self.assertFalse((directory / 'auth.json').exists())
        finally:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)
            for pipe in (process.stdin, process.stdout, process.stderr):
                pipe.close()


if __name__ == '__main__':
    unittest.main()
