"""定时双订单与预热验证；虚拟时钟推进，禁止真实 HTTP 和付款。"""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
import json
import unittest
from unittest.mock import patch

import requests
import duo_killer as shop
import preorder
from protocol import Cancelled
from service import Service
from store_status import StoreClosedError
import test_preorder as plan_tests
import test_installments as installment_tests
from test_preorder import EMPTY, ITEM, params


def unavailable(status=503, retry_after=None):
    response = requests.Response()
    response.status_code = status
    if retry_after:
        response.headers['Retry-After'] = retry_after
    return requests.HTTPError(response=response) if status != 200 else StoreClosedError(response=response)


class ScheduledTests(unittest.TestCase):
    def setUp(self):
        plan_tests.PreorderTests.setUp(self)
        self.target = datetime(2030, 10, 16, 12, tzinfo=timezone.utc)
        self.now = self.target - timedelta(minutes=10)
        self.flows, self.waits, self.submitted_at = [], [], []
        self.test_failure = None
        self.clock = self.stack.enter_context(patch.object(preorder, 'datetime', wraps=datetime))
        self.clock.now.side_effect = lambda *args: self.now
        self.stack.enter_context(patch.object(preorder, 'wait_until', side_effect=self.wait_until))
        self.channel.cancelled.wait.side_effect = self.delay

    def delay(self, seconds):
        self.now += timedelta(seconds=seconds)
        return False

    def wait_until(self, service, target):
        self.waits.append(target)
        self.now = max(self.now, target)

    def make_flow(self, session, policy):
        flow = installment_tests.InstallmentPreorderTests.make_flow(self, session, policy)
        flow.failure = self.test_failure if flow.test_order else self.failure
        original = flow.action

        def action(module, name, data=None):
            if name == 'continueFromReviewToProcess':
                self.submitted_at.append((policy.plan['sku'], self.now))
            return original(module, name, data)

        flow.action = action
        self.flows.append(flow)
        self.flow = flow
        return flow

    def plan(self, **changes):
        return params(target_at=self.target.isoformat(), test_order_authorized=True, **changes)

    def test_test_order_at_minus_five_minutes_then_main_at_target(self):
        original_wait = self.wait_until

        def waiting(service, target):
            if target == self.target - timedelta(seconds=10):
                self.assertFalse((self.task / 'payment.json').exists())
                test = preorder.PlanStore(self.root).data['test_order']
                self.assertEqual(test['state'], 'confirmed')
                self.assertNotIn('payment_url', test['result'])
            original_wait(service, target)

        with patch.object(preorder, 'wait_until', side_effect=waiting):
            result = self.service.run('preorder', self.plan())
        self.assertEqual(self.submitted_at, [(preorder.TEST_SKU, self.target - timedelta(minutes=5)),
                                            (params()['sku'], self.target)])
        self.assertEqual(result['order_number'], 'W123456789')
        self.assertEqual(result['test_order_number'], 'W111111111')
        saved = preorder.PlanStore(self.root).data
        self.assertEqual(saved['baseline'], ['W111111111'])
        self.assertEqual(saved['test_order']['plan']['max_total_cents'], 1099900)
        self.assertFalse(saved['test_order']['plan']['applecare'])
        self.assertIs(self.flows[0].session, self.flows[1].session)
        self.assertEqual(len(self.flows), 2)  # 正式订单使用临开售预热的购物袋。
        self.orders.assert_called_once()
        self.qr.assert_not_called()
        self.assertEqual(json.loads((self.task / 'payment.json').read_text('utf-8'))['order_number'], 'W123456789')
        self.assertEqual(self.service.run('preorder_resume', {}), result)
        self.assertEqual(len(self.submitted_at), 2)

    def test_uncertain_test_never_enters_main_or_repeats_after_restart(self):
        self.test_failure = requests.Timeout('synthetic accepted test response lost')
        result = self.service.run('preorder', self.plan())
        self.assertEqual(result['status'], 'order_uncertain')
        self.assertTrue(result['test_order'])
        self.assertEqual(len(self.submitted_at), 1)
        self.assertFalse((self.task / 'payment.json').exists())
        restarted = Service(self.channel, self.root, self.task, 'test@example.invalid')
        self.assertEqual(restarted.run('preorder_resume', {})['status'], 'order_uncertain')
        self.assertEqual(len(self.submitted_at), 1)
        with self.assertRaisesRegex(ValueError, '阻止再次购买'):
            restarted.run('purchase', {'sku': params()['sku']})
        test = preorder.PlanStore(self.root).data['test_order']
        self.orders.return_value = {**EMPTY, 'orders': [
            {'order_number': 'W111111111', 'items': test['review']['items']}]}
        result = restarted.run('preorder_resume', {})
        self.assertEqual(result['order_number'], 'W123456789')
        self.assertEqual(len(self.submitted_at), 2)
        self.assertEqual(sum(flow.test_order for flow in self.flows), 1)

    def test_confirmed_test_freezes_plan_and_is_not_repeated(self):
        original = self.wait_until

        def stop_before_warmup(service, target):
            if target == self.target - timedelta(seconds=10):
                raise Cancelled()
            original(service, target)

        with patch.object(preorder, 'wait_until', side_effect=stop_before_warmup), self.assertRaises(Cancelled):
            self.service.run('preorder', self.plan())
        self.assertEqual(preorder.PlanStore(self.root).data['test_order']['state'], 'confirmed')
        result = self.service.run('preorder', params(sku='MLH74FE/A'))
        self.assertEqual(result['order_number'], 'W123456789')
        self.assertEqual(len(self.submitted_at), 2)
        self.assertEqual(preorder.PlanStore(self.root).data['plan']['sku'], params()['sku'])

    def test_test_order_does_not_inherit_main_price_limit_or_applecare(self):
        # 两笔订单授权分别限定范围；正式金额上限不能把测试价格变成上限或反之。
        with self.assertRaisesRegex(ValueError, '订单金额超过'):
            self.service.run('preorder', self.plan(max_total='100', applecare=False))
        saved = preorder.PlanStore(self.root).data
        self.assertEqual(saved['test_order']['state'], 'confirmed')
        self.assertEqual(saved['state'], 'paused')
        self.assertEqual(len(self.submitted_at), 1)

    def test_late_new_plan_skips_test_and_still_buys_main(self):
        self.now = self.target - timedelta(seconds=59)
        result = self.service.run('preorder', self.plan())
        self.assertEqual(result['test_order_state'], 'skipped')
        self.assertEqual(self.submitted_at, [(params()['sku'], self.target)])

    def test_cancel_after_test_intent_is_durable(self):
        self.test_failure = Cancelled()
        with self.assertRaises(Cancelled):
            self.service.run('preorder', self.plan())
        saved = preorder.PlanStore(self.root).data
        self.assertEqual(saved['test_order']['state'], 'submitting')
        self.assertEqual(saved['state'], 'paused')
        self.assertEqual(len(self.submitted_at), 1)
        self.assertFalse((self.task / 'payment.json').exists())

    def test_legacy_plan_has_no_extra_order(self):
        self.service.run('preorder', params(target_at=self.target.isoformat()))
        self.assertEqual(self.submitted_at, [(params()['sku'], self.target)])
        self.assertNotIn('test_order', preorder.PlanStore(self.root).data)

    def verify_installment_orders(self, method):
        result = self.service.run('preorder', self.plan(payment_method=method, installment_term=24))
        test = preorder.PlanStore(self.root).data['test_order']
        for value in (test['plan'], test['result'], result):
            self.assertEqual(value['payment_method'], method)
            self.assertEqual(value['installment_term'], 24)
        self.assertEqual(len(self.submitted_at), 2)
        self.assertNotIn('payment_url', test['result'])
        self.qr.assert_not_called()

    def test_cmb_test_and_main_use_the_same_installments(self):
        self.verify_installment_orders('cmb_installment')

    def test_abc_test_and_main_use_the_same_installments(self):
        self.verify_installment_orders('alipay_abc_installment')

    def test_stale_prepared_cart_is_read_again(self):
        original = preorder.warmup

        def stale(*args):
            flow = original(*args)
            flow.prepared_at -= 16
            return flow

        with patch.object(preorder, 'warmup', side_effect=stale):
            self.service.run('preorder', params(target_at=self.target.isoformat()))
        self.assertEqual(len(self.flows), 2)
        self.assertEqual(self.flows[0].submissions, 0)
        self.assertEqual(self.flows[1].submissions, 1)

    def test_no_target_add_before_target_and_prepared_info_used_once(self):
        original_factory = self.make_flow

        def empty_cart(session, policy):
            flow = original_factory(session, policy)
            flow.model['shoppingCart']['items'] = {}
            original_page = flow.page

            def page(url, stage):
                original_page(url, stage)
                if stage == '首次加购后购物袋':
                    flow.model['shoppingCart']['items'] = {'item-phone': deepcopy(ITEM)}

            flow.page = page
            return flow

        self.factory.side_effect = empty_cart
        prepared = {'product': params()['sku'], 'atbtoken': 'synthetic', 'apikey': 'synthetic'}

        def add(session, sku, **kwargs):
            self.assertEqual(self.now, self.target)
            self.assertIs(kwargs['prepared'], prepared)
            return ''

        with patch.object(shop, 'prepare_add_to_bag', return_value=prepared) as prepare, \
                patch.object(shop, '_add_one_to_bag', side_effect=add) as add_call:
            self.service.run('preorder', params(target_at=self.target.isoformat()))
        prepare.assert_called_once()
        add_call.assert_called_once()
        self.assertIsNone(self.flow.prepared_add)

    def test_retry_after_exceeding_window_does_not_make_another_attempt(self):
        self.now = self.target
        response = requests.Response()
        response.status_code = 429
        response.headers['Retry-After'] = '600'
        with patch.object(shop, 'add_to_bag', side_effect=shop.TemporaryCheckoutError('limited', response=response)) as add:
            result = self.service.run('preorder', params(target_at=self.target.isoformat()))
        self.assertEqual(result['state'], 'expired')
        add.assert_called_once()
        self.assertEqual(self.submitted_at, [])

    def test_closed_store_through_preparation_reads_baseline_when_open_and_only_buys_main(self):
        reads = []

        def listing(session):
            reads.append(self.now)
            if self.now < self.target + timedelta(seconds=2):
                raise unavailable()
            return {**EMPTY, 'orders': [{'order_number': 'W999999999'}]}

        self.orders.side_effect = listing
        result = self.service.run('preorder', self.plan())
        self.assertEqual(result['test_order_state'], 'skipped')
        self.assertIn('未验证 Cookie', result['test_order_message'])
        self.assertEqual(self.submitted_at, [(params()['sku'], self.target + timedelta(seconds=2))])
        self.assertEqual(reads[:3], [self.target - timedelta(minutes=10), self.target - timedelta(minutes=5),
                                     self.target - timedelta(seconds=10)])
        saved = preorder.PlanStore(self.root).data
        self.assertEqual(saved['baseline'], ['W999999999'])
        self.assertNotIn('submitted_at', saved['test_order'])
        self.assertEqual(len(self.flows), 1)
        self.qr.assert_not_called()

    def test_browser_503_during_login_preparation_is_deferred_until_open(self):
        from apple_login import BrowserContextError

        def login(session):
            if self.now < self.target:
                error = ValueError('browser store unavailable')
                error.__cause__ = BrowserContextError('store unavailable', status=503)
                raise error

        with patch.object(shop, 'ensure_strong_login', side_effect=login):
            result = self.service.run('preorder', self.plan())
        self.assertEqual(result['test_order_state'], 'skipped')
        self.assertEqual(self.submitted_at, [(params()['sku'], self.target)])
        self.orders.assert_called_once()

    def test_closed_test_cart_is_skipped_and_failed_warmup_reloads_at_target(self):
        original = preorder.read_cart
        reads = []

        def cart(session, policy):
            reads.append(self.now)
            if self.now < self.target:
                raise unavailable(200)
            return original(session, policy)

        with patch.object(preorder, 'read_cart', side_effect=cart):
            result = self.service.run('preorder', self.plan())
        self.assertEqual(result['test_order_state'], 'skipped')
        self.assertEqual(reads, [self.target - timedelta(minutes=5), self.target - timedelta(seconds=10), self.target])
        self.assertEqual(self.submitted_at, [(params()['sku'], self.target)])

    def test_http_503_after_test_submission_only_reconciles_and_blocks_main(self):
        self.test_failure = unavailable()
        result = self.service.run('preorder', self.plan())
        self.assertEqual(result['status'], 'order_uncertain')
        self.assertEqual(preorder.PlanStore(self.root).data['test_order']['state'], 'uncertain')
        self.assertEqual(self.submitted_at, [(preorder.TEST_SKU, self.target - timedelta(minutes=5))])
        self.service.run('preorder_resume', {})
        self.assertEqual(len(self.submitted_at), 1)

    def test_skipped_test_is_not_recreated_after_restart(self):
        original = self.wait_until

        def stop_at_warmup(service, target):
            if target == self.target - timedelta(seconds=10):
                raise Cancelled()
            original(service, target)

        with patch.object(preorder, 'read_cart', side_effect=unavailable()), \
                patch.object(preorder, 'wait_until', side_effect=stop_at_warmup), self.assertRaises(Cancelled):
            self.service.run('preorder', self.plan())
        self.assertEqual(preorder.PlanStore(self.root).data['test_order']['state'], 'skipped')
        restarted = Service(self.channel, self.root, self.task, 'test@example.invalid')
        result = restarted.run('preorder_resume', {})
        self.assertEqual(result['test_order_state'], 'skipped')
        self.assertEqual(self.submitted_at, [(params()['sku'], self.target)])

    def test_test_retry_after_carries_across_warmup_and_main_start(self):
        original = preorder.read_cart
        reads = []

        def cart(session, policy):
            reads.append(self.now)
            if self.now < self.target:
                raise unavailable(retry_after='310')
            return original(session, policy)

        with patch.object(preorder, 'read_cart', side_effect=cart):
            self.service.run('preorder', self.plan())
        self.assertEqual(reads, [self.target - timedelta(minutes=5), self.target + timedelta(seconds=10)])
        self.assertEqual(self.submitted_at, [(params()['sku'], self.target + timedelta(seconds=10))])

    def test_startup_retry_after_beyond_window_expires_without_more_requests(self):
        self.orders.side_effect = unavailable(retry_after='1000')
        result = self.service.run('preorder', self.plan())
        self.assertEqual(result['state'], 'expired')
        self.assertEqual(result['test_order_state'], 'skipped')
        self.orders.assert_called_once()
        self.assertEqual(self.submitted_at, [])

    def test_explicit_authentication_failure_during_test_still_stops(self):
        from apple_login import LoginError
        error = ValueError('authentication rejected')
        error.__cause__ = LoginError(str(error), status=401, codes=('-20101',))
        with patch.object(preorder, 'read_cart', side_effect=error), self.assertRaises(ValueError):
            self.service.run('preorder', self.plan())
        self.assertNotEqual(preorder.PlanStore(self.root).data['test_order']['state'], 'skipped')
        self.assertEqual(self.submitted_at, [])

    def test_unknown_or_forbidden_warmup_is_not_treated_as_closed(self):
        with patch.object(preorder, 'warmup', side_effect=unavailable(403)), self.assertRaises(requests.HTTPError):
            self.service.run('preorder', params(target_at=self.target.isoformat()))
        self.assertEqual(self.submitted_at, [])


class RetryTests(unittest.TestCase):
    def test_first_window_and_later_cadence(self):
        target = datetime(2030, 10, 16, 12, tzinfo=timezone.utc)
        for seconds, expected in ((0, 1), (29, 1), (30, 2), (119, 2), (120, 5)):
            with self.subTest(seconds=seconds):
                self.assertEqual(preorder.retry_delay(requests.Timeout(), target, target + timedelta(seconds=seconds)), expected)

    def test_retry_after_seconds_date_and_permanent_error(self):
        now = datetime(2030, 10, 16, 12, tzinfo=timezone.utc)
        response = requests.Response()
        response.status_code = 429
        error = requests.HTTPError(response=response)
        for value in ('60', format_datetime(now + timedelta(seconds=60), usegmt=True)):
            response.headers['Retry-After'] = value
            self.assertEqual(preorder.retry_delay(error, now, now), 60)
        response.status_code = 403
        with self.assertRaises(requests.HTTPError):
            preorder.retry_delay(error, now, now)


if __name__ == '__main__':
    unittest.main()
