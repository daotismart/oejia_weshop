# coding=utf-8

from openerp import models, fields, api

from .. import defs


class SaleOrderLine(models.Model):

    _inherit = 'sale.order.line'

    reputation = fields.Integer('评价')
    reputation_remark = fields.Text('详细评价')

