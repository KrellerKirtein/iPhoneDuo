"""AC+ 离线契约检查；官网网络及订单创建均不执行。"""
from copy import deepcopy
from datetime import datetime, timezone
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import requests
import alipay_qr
import interaction
import duo_killer as shop
import preorder
from protocol import Channel
from service import Service


SKU = 'MK2U4CH/A'
CHILD = {'d': {'isChildItem': True, 'name': 'AppleCare+ 服务计划', 'sku': 'CARE001/A'}}


class CareFlow:
    """模拟官网购物袋的附加商品和套餐编辑动作。"""
    def __init__(self, attached=False, modal=False):
        self.item = {'d': {'sku': SKU}, 'itemQuantity': {'d': {'quantity': 1}},
                     'inlineRecommendations': {'care': {'d': {'title': 'AppleCare+ 服务计划',
                                                              'appleCarePartNumber': 'CARE001/A'},
                                                       'a': {'add': {'url': '/shop/bagx'}}}}}
        if attached:
            self.item['care'] = deepcopy(CHILD)
        if modal:
            self.item['a'] = {'changeAppleCareModal': {'url': '/shop/bagx'}}
        self.groups = [{'displayName': 'AppleCare+', 'values': [{'value': 'CARE001/A'}]},
                       {'displayName': '不购买', 'value': 'none'}]
        self.model = {'shoppingCart': {'items': {'item-phone': self.item}}}
        self.calls = []
        self.ignore_change = False

    def require(self, path):
        return shop.field(self.model, path)

    def page(self, url, stage):
        self.calls.append(('page', stage))

    def action(self, module, name, data=None):
        self.calls.append((module, name, data))
        if name == 'changeAppleCareModal':
            self.item['changeAppleCare'] = {'d': {'appleCareGroups': self.groups}}
        elif not self.ignore_change:
            if name == 'updateAppleCareSelection' and data[module + '.appleCarePartNumber'] == 'none':
                self.item.pop('care', None)
            else:
                self.item['care'] = deepcopy(CHILD)


class AppleCareTests(unittest.TestCase):
    def setUp(self):
        for target, names in ((shop, ('ROOT', 'AUTH_PATH', 'LOG_PATH', 'PAYMENT_ROOT')),
                              (alipay_qr, ('ROOT',)), (interaction, ('handler',))):
            for name in names:
                state = patch.object(target, name, getattr(target, name))
                state.start()
                self.addCleanup(state.stop)
        network = patch.object(requests.Session, 'send', side_effect=AssertionError('禁止真实网络和下单'))
        network.start()
        self.addCleanup(network.stop)

    def test_inline_add_is_verified_and_idempotent(self):
        flow = CareFlow()
        shop.configure_applecare(flow, SKU, True)
        self.assertEqual(flow.calls[0], ('shoppingCart.items.item-phone.inlineRecommendations.care', 'add',
                         {'shoppingCart.items.item-phone.inlineRecommendations.care.appleCarePartNumber': 'CARE001/A'}))
        self.assertEqual(flow.applecare_selection, (SKU, ['CARE001/A']))
        before = len(flow.calls)
        shop.configure_applecare(flow, SKU, True)
        self.assertEqual(len(flow.calls), before)

    def test_modal_add_and_remove(self):
        for selected in (True, False):
            flow = CareFlow(attached=not selected, modal=True)
            shop.configure_applecare(flow, SKU, selected)
            self.assertEqual(flow.calls[1][2], {'shoppingCart.items.item-phone.changeAppleCare.appleCarePartNumber':
                                              'CARE001/A' if selected else 'none'})
            self.assertEqual(bool(shop.applecare_items(flow.item)), selected)

    def test_default_off_does_not_modify_cart(self):
        flow = CareFlow()
        shop.configure_applecare(flow, SKU, False)
        self.assertEqual(flow.calls, [])
        self.assertEqual(shop.applecare_items(flow.item), [])

    def test_missing_or_ambiguous_plan_stops(self):
        for mode in ('missing', 'inline', 'modal'):
            flow = CareFlow(modal=mode == 'modal')
            if mode == 'missing':
                flow.item.pop('inlineRecommendations')
            elif mode == 'inline':
                flow.item['inlineRecommendations']['care']['d']['options'] = [{'value': 'A'}, {'value': 'B'}]
            else:
                flow.groups[0]['values'].append({'value': 'CARE002/A'})
            with self.assertRaisesRegex(ValueError, 'AppleCare'):
                shop.configure_applecare(flow, SKU, True)
            self.assertFalse(shop.applecare_items(flow.item))

    def test_unconfirmed_add_and_unsupported_remove_stop(self):
        flow = CareFlow()
        flow.ignore_change = True
        with self.assertRaisesRegex(ValueError, '状态与选择不一致'):
            shop.configure_applecare(flow, SKU, True)
        with self.assertRaisesRegex(ValueError, '无法自动取消'):
            shop.configure_applecare(CareFlow(attached=True), SKU, False)

    def test_review_must_retain_exact_coverage(self):
        flow = CareFlow()
        shop.configure_applecare(flow, SKU, True)
        shop.verify_applecare(flow, {'items': {'phone': deepcopy(flow.item)}})
        for replacement in (None, 'CARE002/A'):
            review = deepcopy(flow.item)
            if replacement:
                review['care']['d']['sku'] = replacement
            else:
                review.pop('care')
            with self.assertRaisesRegex(ValueError, 'AppleCare 与购买选择不一致'):
                shop.verify_applecare(flow, {'phone': review})

    def test_unselected_coverage_cannot_appear_in_review(self):
        flow = CareFlow()
        shop.configure_applecare(flow, SKU, False)
        flow.item['care'] = deepcopy(CHILD)
        with self.assertRaisesRegex(ValueError, 'AppleCare 与购买选择不一致'):
            shop.verify_applecare(flow, flow.model['shoppingCart']['items'])

    def test_plan_preserves_boolean_and_old_default(self):
        data = {'sku': SKU, 'authorized': True, 'target_at': datetime.now(timezone.utc).isoformat(),
                'max_total': '23000'}
        self.assertFalse(preorder.validate_plan(data)['applecare'])
        self.assertTrue(preorder.validate_plan({**data, 'applecare': True})['applecare'])
        for value in ('false', 1, None):
            with self.assertRaisesRegex(ValueError, 'AppleCare'):
                preorder.validate_plan({**data, 'applecare': value})
        with patch.object(shop, 'ensure_auth_file') as auth:
            with self.assertRaisesRegex(ValueError, 'AppleCare'):
                shop.purchase_sku(SKU, applecare='false')
            auth.assert_not_called()

    def test_service_forwards_selection(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            service = Service(Channel('a', 't', io.StringIO()), root, root, 'test@example.invalid', Mock())
            with patch.object(service, 'ensure_auth_file'), patch.object(service, 'persist_password'), \
                    patch.object(shop, 'purchase_sku', return_value={'success': True}) as buy:
                service.run('purchase', {'sku': SKU, 'quantity': 1, 'applecare': True})
                buy.assert_called_once_with(SKU, quantity=1, applecare=True,
                                            payment_method='alipay', installment_term=None)
