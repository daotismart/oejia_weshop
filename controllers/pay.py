# -*- coding: utf-8 -*-

import json

from odoo import http, exceptions
from odoo.http import request

from .. import defs
from .base import BaseController
import string,time,random
from hashlib import md5
import logging

_logger = logging.getLogger(__name__)


class WxappPay(http.Controller, BaseController):

    @http.route('/<string:sub_domain>/pay/wx/wxapp',
                auth='public', methods=['POST'], csrf=False, type='http')
    def wxapp(self, sub_domain, **kwargs):
        token = kwargs.pop('token', None)
        try:
            res, wechat_user, entry = self._check_user(sub_domain, token)
            if res:return res
            #weixin = request.env['payment.acquirer'].sudo().search([('provider', '=', 'weixin')])
            _logger.info('>>> order_dict%s, %s, %s',res, wechat_user,entry)
            weixin = entry #request.env['wxapp.config'].sudo().search([])[0]
            _logger.info('>>> order_dict %s,[%s,%s,%s,%s]', weixin,weixin.wechat_pay_id,weixin.wechat_pay_secret,weixin.app_id,weixin.secret)
            
            # [{"goodsId":1,"number":3,"propertyChildIds":"1:1,2:4,","logisticsType":0, "inviter_id":0}]
            money = int(kwargs.pop('money'))
            remark = kwargs.pop('remark', '')
            payName = kwargs.pop('payName', '')
            nextAction = json.loads(kwargs.pop('nextAction'))
            
            _logger.info('>>> order_dict [%s,%s,%s,%s]', money ,remark ,payName ,nextAction)
            from wechatpy.pay import WeChatPay
            pay = WeChatPay(appid=weixin.app_id,  api_key=weixin.wechat_pay_secret, mch_id=weixin.wechat_pay_id, sub_appid=weixin.app_id)
            res = pay.order.create(
                trade_type="JSAPI",
                body=payName,
                total_fee=money,
                user_id=wechat_user.open_id,
                notify_url='http://www.daotismart.com/%s/pay/wx/callback' % (sub_domain)
            )
            _logger.info('>>> order_dict %s', res)
            prepay_id = res.get("prepay_id")
            statusValue = {'SUCCESS':defs.PaymentStatus.success,
                'FAIL':defs.PaymentStatus.fail
            }
            payment = {
                "openid": wechat_user.open_id,
                "order_id": nextAction['id'],
                "payment_number": prepay_id,
                "result_code": res.get("result_code"),
                "settlement_total_fee": money,
                "total_fee": money,
                "transaction_id": prepay_id,
                "wechat_user_id": wechat_user.id,
                "status": statusValue[res.get("result_code")]
            }
            request.env(user=1)['wxapp.payment'].create(payment)
            
            order = request.env['sale.order'].sudo().search([
                ('partner_id', '=', wechat_user.partner_id.id),
                ('id', '=', int(nextAction['id']))
            ])

            if not order:
                return self.res_err(404)
            order.check_paid()

            #order.write({'customer_status': 'padding'})


            params = {
                    "appId": weixin.app_id,
                    "timeStamp": str(int(time.time())),
                    "nonceStr": ''.join(random.sample(string.ascii_letters + string.digits, 32)),
                    "package": 'prepay_id=%s'%(prepay_id),
                    "signType": "MD5",
            }
            strs = '&'.join(['{}={}'.format(key, params.get(key))
                    for key in sorted(params.keys()) if params.get(key)]) + "&key={}".format(weixin.wechat_pay_secret)
            _logger.info('>>> strs:[%s]', strs)
            paySign = md5(strs.encode("utf-8")).hexdigest().upper()
            params['sign'] = paySign

            return self.res_ok(params)

        except Exception as e:
            _logger.exception(e)
            return self.res_err(-1, e.name)

    def parse_goods_json(self, goods_json, province_id, city_id, district_id, calculate):
        """
        :param goods_json: dict
        :param province_id: 省
        :param city_id: 市
        :param district_id: 区
        :return: goods_price, logistics_price, total, goods_list
        """
        # [{"goodsId":1,"number":3,"propertyChildIds":"1:1,2:4,","logisticsType":0, "inviter_id":0}]
        goods_price, logistics_price = 0.0, 0.0
        goods_list = []

        goods_id_set = set(map(lambda r: r['goodsId'], goods_json))
        product_list = []
        for data in goods_json:
            rs = request.env['product.product'].sudo().search([
                ('product_tmpl_id', '=', data['goodsId']),
                ('attr_val_str', '=', data['propertyChildIds'])
            ])
            product_list += [p for p in rs]

        template_list = request.env['product.template'].sudo().search([
            ('id', 'in', list(goods_id_set)),
            ('wxapp_published', '=', True)
        ])
        template_dict = {template.id: template for template in template_list}

        if set(template_dict.keys()) - goods_id_set:
            raise exceptions.ValidationError('订单中包含已下架的商品')

        for each_goods in goods_json:
            property_child_ids = each_goods.get('propertyChildIds')
            amount = each_goods['number']
            transport_type = each_goods['logisticsType']
            template = template_dict[each_goods['goodsId']]

            each_goods_price, each_goods_total, property_str, product = self.calculate_goods_fee(template, amount, property_child_ids, calculate)
            each_logistics_price = self.calculate_logistics_fee(template, amount, transport_type, province_id, city_id, district_id)
            goods_list.append({
                'product_id': product.id,
                'price_unit': each_goods_price,
                'product_uom_qty': amount,
            })
            goods_price += each_goods_total
            logistics_price += each_logistics_price

        return goods_price, logistics_price, goods_price + logistics_price, goods_list

    def calculate_goods_fee(self, goods, amount, property_child_ids, calculate):
        _logger.info('>>> calculate_goods_fee %s %s %s', goods, amount, property_child_ids)
        property_str = ''

        if 1:#property_child_ids:
            property_child_ids = property_child_ids or ''
            product = request.env['product.product'].sudo().search([
                ('product_tmpl_id', '=', goods.id),
                ('attr_val_str', '=', property_child_ids)
            ])
            if not product:
                raise exceptions.ValidationError('商品不存在！')

            price = product.get_present_price()
            total = price * amount
            property_str = product.name

            stores = product.get_present_qty() - amount
            if not property_child_ids:
                stores = goods.get_present_qty() - amount

            if stores < 0:
                raise exceptions.ValidationError('库存不足！')
            if stores == 0:
                # todo 发送库存空预警
                pass
            if not calculate:
                product.sudo().change_qty(-amount)
                if not property_child_ids:
                    goods.sudo().change_qty(-amount)

        return price, total, property_str, product

    def calculate_logistics_fee(self, goods, amount, transport_type, province_id, city_id, district_id):
        return 0


    @http.route('/<string:sub_domain>/pay/alipay/semiAutomatic/payurl', auth='public', method=['GET', 'POST'], csrf=False)
    def statistics(self, sub_domain, token=None, **kwargs):
        '''
        closed = ('closed', u'已关闭')
        unpaid = ('unpaid', u'待支付')
        pending = ('pending', u'待发货')
        unconfirmed = ('unconfirmed', u'待收货')
        unevaluated = ('unevaluated', u'待评价')
        completed = ('completed', u'已完成')
        '''
        try:
            res, wechat_user, entry = self._check_user(sub_domain, token)
            if res:return res

            orders = request.env['sale.order'].sudo().search([('partner_id', '=', wechat_user.partner_id.id)])
            order_statistics_dict = {order_status: 0 for order_status in defs.OrderStatus.attrs.keys()}
            for each_order in orders:
                order_statistics_dict[each_order.customer_status] += 1

            data = {
                "count_id_no_reputation": order_statistics_dict['unevaluated'],
                "count_id_no_transfer": order_statistics_dict['pending'],
                "count_id_close": order_statistics_dict['closed'],
                "count_id_no_pay": order_statistics_dict['unpaid'],
                "count_id_no_confirm": order_statistics_dict['unconfirmed'],
                "count_id_success": order_statistics_dict['completed']
            }
            return self.res_ok(data)

        except Exception as e:
            _logger.exception(e)
            return self.res_err(-1, e.name)


    @http.route('/<string:sub_domain>/pay/wx/callback', auth='public', method=['GET', 'POST'], csrf=False)
    def list(self,sub_domain, **kwargs):
        try:
            #res, wechat_user, entry = self._check_user(sub_domain, token)
            #if res:return res

            _logger.info('>>> callback data:[%s]',kwargs)
            if(kwargs == {}):
                return self.res_ok({
                    "return_code": 'FAIL',
                    "return_msg": 'NULL'
                })
                
            if(kwargs['return_code'] == 'SUCCESS'):
                data = {
                    "return_code": 'SUCCESS',
                    "return_msg": 'OK'
                }
            else:
                data = {
                    "return_code": 'FAIL',
                    "return_msg": kwargs['return_msg']
                }
                 
            return self.res_ok(data)

        except Exception as e:
            _logger.exception(e)
            return self.res_err(-1, e.name)


    @http.route('/<string:sub_domain>/pay/alipay/callback', auth='public', method=['GET'])
    def detail(self, sub_domain, token=None, id=None, **kwargs):
        order_id = id
        try:
            res, wechat_user, entry = self._check_user(sub_domain, token)
            if res:return res

            if not order_id:
                return self.res_err(300)

            order = request.env['sale.order'].sudo().search([
                ('partner_id', '=', wechat_user.partner_id.id),
                ('id', '=', int(order_id))
            ])

            if not order:
                return self.res_err(404)

            delivery_product_id = request.env.ref('oejia_weshop.product_product_delivery_weshop').id
            data = {
                "code": 0,
                "data": {
                    "orderInfo": {
                        "amount": order.goods_price,
                        "amountLogistics": order.logistics_price,
                        "amountReal": order.amount_total,
                        "dateAdd": order.create_date,
                        "dateUpdate": order.write_date,
                        "goodsNumber": order.number_goods,
                        "id": order.id,
                        "orderNumber": order.name,
                        "remark": order.note,
                        "status": defs.OrderResponseStatus.attrs[order.customer_status],
                        "statusStr": defs.OrderStatus.attrs[order.customer_status],
                        "type": 0,
                        "uid": 1,#user.id,
                        "userId": wechat_user.id
                    },
                    "goods": [
                        {
                            "amount": each_goods.price_unit,
                            "goodsId": each_goods.product_id.product_tmpl_id.id,
                            "goodsName": each_goods.name,
                            "id": each_goods.id,
                            "number": each_goods.product_uom_qty,
                            "orderId": order.id,
                            "pic": each_goods.product_id.product_tmpl_id.get_main_image(),
                            "property": each_goods.product_id.get_property_str(),
                        } for each_goods in order.order_line if each_goods.product_id.id!=delivery_product_id
                    ],
                    "logistics": {
                        "address": order.address,
                        "cityId": order.city_id.id,
                        "code": order.zipcode,
                        "dateUpdate": order.write_date,
                        "districtId": order.district_id.id or 0,
                        "linkMan": order.linkman,
                        "mobile": order.mobile,
                        "provinceId": order.province_id.id,
                        "shipperCode": order.shipper_id.code if order.shipper_id else '',
                        "shipperName": order.shipper_id.name if order.shipper_id else '',
                        "status": 0 if order.shipper_id else '',
                        "trackingNumber": order.shipper_no if order.shipper_no else ''
                    },
                },
                "msg": "success"
            }
            if order.shipper_no:
                self.build_traces(order, data)

            return self.res_ok(data["data"])

        except Exception as e:
            _logger.exception(e)
            return self.res_err(-1, e.name)

    def build_traces(self, order, data):
        pass

    @http.route('/<string:sub_domain>/order/close', auth='public', method=['GET', 'POST'], csrf=False)
    def close(self, sub_domain, token=None, orderId=None, **kwargs):
        order_id = orderId
        try:
            res, wechat_user, entry = self._check_user(sub_domain, token)
            if res:return res

            if not order_id:
                return self.res_err(300)

            order = request.env['sale.order'].sudo().search([
                ('partner_id', '=', wechat_user.partner_id.id),
                ('id', '=', int(order_id))
            ])

            if not order:
                return self.res_err(404)

            if order.state=='sale':
                return self.res_err(-99, u'该订单已被确认，无法取消')

            #order.write({'customer_status': 'closed'})
            order.action_cancel()

            #mail_template = request.env.ref('wechat_mall_order_closed')
            #mail_template.sudo().send_mail(order.id, force_send=True, raise_exception=False)

            return self.res_ok()

        except Exception as e:
            _logger.exception(e)
            return self.res_err(-1, e.name)


    @http.route('/<string:sub_domain>/order/delivery', auth='public', method=['GET'])
    def delivery(self, sub_domain, token=None, orderId=None, **kwargs):
        '''
        确认收货接口
        '''
        order_id = orderId
        try:
            res, wechat_user, entry = self._check_user(sub_domain, token)
            if res:return res

            if not order_id:
                return self.res_err(300)

            order = request.env['sale.order'].sudo().search([
                ('partner_id', '=', wechat_user.partner_id.id),
                ('id', '=', int(order_id))
            ])

            if not order:
                return self.res_err(404)

            order.write({'customer_status': 'unevaluated'})

            #mail_template = request.env.ref('wechat_mall_order_confirmed')
            #mail_template.sudo().send_mail(order.id, force_send=True, raise_exception=False)

            return self.res_ok()

        except Exception as e:
            _logger.exception(e)
            return self.res_err(-1, e.name)


    @http.route('/<string:sub_domain>/order/reputation', auth='public', method=['GET'])
    def reputation(self, sub_domain, token=None, order_id=None, reputation=2, **kwargs):
        '''
        评论接口
        {
            "token": "xxx",
            "orderId": "4",
            "reputations": [{
                "id": "4",
                "reputation": "2",
                "remark": "xxx"
            }]
        }
        '''
        try:
            post_json = json.loads(kwargs.pop('postJsonString'))
            token = post_json.get('token',None)
            order_id = post_json.get('orderId',None)
            reputations = post_json.get('reputations',[])

            res, wechat_user, entry = self._check_user(sub_domain, token)
            if res:return res

            if not order_id:
                return self.res_err(300)

            order = request.env['sale.order'].sudo().search([
                ('partner_id', '=', wechat_user.partner_id.id),
                ('id', '=', int(order_id))
            ])

            if not order:
                return self.res_err(404)

            order.write({'customer_status': 'completed'})

            for reputation in reputations:
                # 保存评论
                pass

            return request.make_response(json.dumps({'code': 0, 'msg': 'success'}))

        except Exception as e:
            _logger.exception(e)
            return self.res_err(-1, e.name)



