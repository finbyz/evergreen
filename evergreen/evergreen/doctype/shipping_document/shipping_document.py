# -*- coding: utf-8 -*-
# Copyright (c) 2019, Finbyz Tech Pvt Ltd and contributors
# For license information, please see license.txt

from __future__ import unicode_literals
import frappe
import json
import frappe.utils
from frappe.utils import cstr, flt, getdate, comma_and, cint
from frappe import _
from frappe.model.utils import get_fetch_values
from frappe.model.mapper import get_mapped_doc
# from erpnext.stock.stock_balance import update_bin_qty, get_reserved_qty
from frappe.desk.notifications import clear_doctype_notifications
from frappe.contacts.doctype.address.address import get_company_address
from erpnext.controllers.selling_controller import SellingController
from frappe.automation.doctype.auto_repeat.auto_repeat import get_next_schedule_date
from erpnext.selling.doctype.customer.customer import check_credit_limit

form_grid_templates = {
	"items": "templates/form_grid/item_grid.html"
}

class WarehouseRequired(frappe.ValidationError): pass

class ShippingDocument(SellingController):
	def __init__(self, *args, **kwargs):
		super(ShippingDocument, self).__init__(*args, **kwargs)

	def validate(self):
		# super(ShippingDocument, self).validate()

		self.validate_order_type()
		self.validate_delivery_date()
		# self.validate_proj_cust()
		self.validate_po()
		self.validate_uom_is_integer("stock_uom", "stock_qty")
		self.validate_uom_is_integer("uom", "qty")
		self.validate_for_items()
		self.validate_warehouse()
		self.validate_drop_ship()

		

		from erpnext.stock.doctype.packed_item.packed_item import make_packing_list
		make_packing_list(self)

		# Changes
		# self.validate_with_previous_doc()
		# self.set_status()

		if not self.billing_status: self.billing_status = 'Not Billed'
		if not self.delivery_status: self.delivery_status = 'Not Delivered'

	def validate_po(self):
		# validate p.o date v/s delivery date
		if self.po_date:
			for d in self.get("items"):
				if d.delivery_date and getdate(self.po_date) > getdate(d.delivery_date):
					frappe.throw(_("Row #{0}: Expected Delivery Date cannot be before Purchase Order Date")
						.format(d.idx))

		if self.po_no and self.customer:
			so = frappe.db.sql("select name from `tabSales Order` \
				where ifnull(po_no, '') = %s and name != %s and docstatus < 2\
				and customer = %s", (self.po_no, self.name, self.customer))
			if so and so[0][0] and not cint(frappe.db.get_single_value("Selling Settings",
				"allow_against_multiple_purchase_orders")):
				frappe.msgprint(_("Warning: Sales Order {0} already exists against Customer's Purchase Order {1}").format(so[0][0], self.po_no))

	def validate_for_items(self):
		check_list = []
		for d in self.get('items'):
			check_list.append(cstr(d.item_code))

			# used for production plan
			d.transaction_date = self.transaction_date

			# Changes
			# tot_avail_qty = frappe.db.sql("select projected_qty from `tabBin` \
			# 	where item_code = %s and warehouse = %s", (d.item_code, d.warehouse))
			# d.projected_qty = tot_avail_qty and flt(tot_avail_qty[0][0]) or 0

		# check for same entry multiple times
		unique_chk_list = set(check_list)
		if len(unique_chk_list) != len(check_list) and \
			not cint(frappe.db.get_single_value("Selling Settings", "allow_multiple_items")):
			frappe.msgprint(_("Same item has been entered multiple times"),
				title=_("Warning"), indicator='orange')

	def product_bundle_has_stock_item(self, product_bundle):
		"""Returns true if product bundle has stock item"""
		ret = len(frappe.db.sql("""select i.name from tabItem i, `tabProduct Bundle Item` pbi
			where pbi.parent = %s and pbi.item_code = i.name and i.is_stock_item = 1""", product_bundle))
		return ret

	def validate_sales_mntc_quotation(self):
		for d in self.get('items'):
			if d.prevdoc_docname:
				res = frappe.db.sql("select name from `tabQuotation` where name=%s and order_type = %s",
					(d.prevdoc_docname, self.order_type))
				if not res:
					frappe.msgprint(_("Quotation {0} not of type {1}")
						.format(d.prevdoc_docname, self.order_type))

	def validate_order_type(self):
		super(ShippingDocument, self).validate_order_type()

	def validate_delivery_date(self):
		if self.order_type == 'Sales':
			if not self.delivery_date:
				delivery_date_list = [d.delivery_date for d in self.get("items") if d.delivery_date]
				self.delivery_date = max(delivery_date_list) if delivery_date_list else None
			if self.delivery_date:
				for d in self.get("items"):
					if not d.delivery_date:
						d.delivery_date = self.delivery_date

					if getdate(self.transaction_date) > getdate(d.delivery_date):
						frappe.msgprint(_("Expected Delivery Date should be after Sales Order Date"),
							indicator='orange', title=_('Warning'))
			else:
				frappe.throw(_("Please enter Delivery Date"))

		# Changes
		# self.validate_sales_mntc_quotation()

	# Changes
	# def validate_proj_cust(self):
	# 	if self.project and self.customer_name:
	# 		res = frappe.db.sql("""select name from `tabProject` where name = %s
	# 			and (customer = %s or ifnull(customer,'')='')""",
	# 				(self.project, self.customer))
	# 		if not res:
	# 			frappe.throw(_("Customer {0} does not belong to project {1}").format(self.customer, self.project))

	def validate_warehouse(self):
		super(ShippingDocument, self).validate_warehouse()

		for d in self.get("items"):
			if (frappe.db.get_value("Item", d.item_code, "is_stock_item") == 1 or
				(self.has_product_bundle(d.item_code) and self.product_bundle_has_stock_item(d.item_code))) \
				and not d.warehouse and not cint(d.delivered_by_supplier):
				frappe.throw(_("Delivery warehouse required for stock item {0}").format(d.item_code),
					WarehouseRequired)

	# Changes
	# def validate_with_previous_doc(self):
	# 	super(ShippingDocument, self).validate_with_previous_doc({
	# 		"Quotation": {
	# 			"ref_dn_field": "prevdoc_docname",
	# 			"compare_fields": [["company", "="], ["currency", "="]]
	# 		}
	# 	})

	def update_enquiry_status(self, prevdoc, flag):
		enq = frappe.db.sql("select t2.prevdoc_docname from `tabQuotation` t1, `tabQuotation Item` t2 where t2.parent = t1.name and t1.name=%s", prevdoc)
		if enq:
			frappe.db.sql("update `tabOpportunity` set status = %s where name=%s",(flag,enq[0][0]))

	# Changes
	# def update_prevdoc_status(self, flag):
	# 	for quotation in list(set([d.prevdoc_docname for d in self.get("items")])):
	# 		if quotation:
	# 			doc = frappe.get_doc("Quotation", quotation)
	# 			if doc.docstatus==2:
	# 				frappe.throw(_("Quotation {0} is cancelled").format(quotation))

	# 			doc.set_status(update=True)
	# 			doc.update_opportunity()

	def validate_drop_ship(self):
		for d in self.get('items'):
			if d.delivered_by_supplier and not d.supplier:
				frappe.throw(_("Row #{0}: Set Supplier for item {1}").format(d.idx, d.item_code))

	def on_submit(self):
		self.validate_qty()
		self.check_credit_limit()
		# Changes
		# self.update_reserved_qty()

		frappe.get_doc('Authorization Control').validate_approving_authority(self.doctype, self.company, self.base_grand_total, self)
		# Changes
		# self.update_project()
		# self.update_prevdoc_status('submit')

	def validate_qty(self):
		for row in self.items:
			if row.so_detail:
				so_qty = frappe.db.get_value("Sales Order Item", row.so_detail, 'qty')
				qty = frappe.db.sql("""select sum(qty) from `tabShipping Document Item`
					where so_detail = %s and docstatus = 1""", row.so_detail)[0][0]

				if qty > so_qty:
					frappe.throw(_("Total shipping qty {} exceeds Sales Order qty {}".format(qty, so_qty)))

	def on_cancel(self):
		# Cannot cancel closed SO
		if self.status == 'Closed':
			frappe.throw(_("Closed order cannot be cancelled. Unclose to cancel."))

		# Changes
		# self.check_nextdoc_docstatus()
		# self.update_reserved_qty()
		# self.update_project()
		# self.update_prevdoc_status('cancel')

		frappe.db.set(self, 'status', 'Cancelled')

	# Changes
	# def update_project(self):
	# 	project_list = []
	# 	if self.project:
	# 		project = frappe.get_doc("Project", self.project)
	# 		project.flags.dont_sync_tasks = True
	# 		project.update_sales_amount()
	# 		project.save()
	# 		project_list.append(self.project)

	def check_credit_limit(self):
		# if bypass credit limit check is set to true (1) at sales order level,
		# then we need not to check credit limit and vise versa
		if not cint(frappe.db.get_value("Customer", self.customer, "bypass_credit_limit_check_at_sales_order")):
			check_credit_limit(self.customer, self.company)

	# Changes
	# def check_nextdoc_docstatus(self):
	# 	# Checks Delivery Note
	# 	submit_dn = frappe.db.sql_list("""
	# 		select t1.name
	# 		from `tabDelivery Note` t1,`tabDelivery Note Item` t2
	# 		where t1.name = t2.parent and t2.against_sales_order = %s and t1.docstatus = 1""", self.name)

	# 	if submit_dn:
	# 		frappe.throw(_("Delivery Notes {0} must be cancelled before cancelling this Sales Order")
	# 			.format(comma_and(submit_dn)))

	# 	# Checks Sales Invoice
	# 	submit_rv = frappe.db.sql_list("""select t1.name
	# 		from `tabSales Invoice` t1,`tabSales Invoice Item` t2
	# 		where t1.name = t2.parent and t2.sales_order = %s and t1.docstatus = 1""",
	# 		self.name)

	# 	if submit_rv:
	# 		frappe.throw(_("Sales Invoice {0} must be cancelled before cancelling this Sales Order")
	# 			.format(comma_and(submit_rv)))

	# 	#check maintenance schedule
	# 	submit_ms = frappe.db.sql_list("""
	# 		select t1.name
	# 		from `tabMaintenance Schedule` t1, `tabMaintenance Schedule Item` t2
	# 		where t2.parent=t1.name and t2.sales_order = %s and t1.docstatus = 1""", self.name)

	# 	if submit_ms:
	# 		frappe.throw(_("Maintenance Schedule {0} must be cancelled before cancelling this Sales Order")
	# 			.format(comma_and(submit_ms)))

	# 	# check maintenance visit
	# 	submit_mv = frappe.db.sql_list("""
	# 		select t1.name
	# 		from `tabMaintenance Visit` t1, `tabMaintenance Visit Purpose` t2
	# 		where t2.parent=t1.name and t2.prevdoc_docname = %s and t1.docstatus = 1""",self.name)

	# 	if submit_mv:
	# 		frappe.throw(_("Maintenance Visit {0} must be cancelled before cancelling this Sales Order")
	# 			.format(comma_and(submit_mv)))

	# 	# check production order
	# 	pro_order = frappe.db.sql_list("""
	# 		select name
	# 		from `tabProduction Order`
	# 		where sales_order = %s and docstatus = 1""", self.name)

	# 	if pro_order:
	# 		frappe.throw(_("Production Order {0} must be cancelled before cancelling this Sales Order")
	# 			.format(comma_and(pro_order)))

	def check_modified_date(self):
		mod_db = frappe.db.get_value("Shipping Document", self.name, "modified")
		date_diff = frappe.db.sql("select TIMEDIFF('%s', '%s')" %
			( mod_db, cstr(self.modified)))
		if date_diff and date_diff[0][0]:
			frappe.throw(_("{0} {1} has been modified. Please refresh.").format(self.doctype, self.name))

	def update_status(self, status):
		self.check_modified_date()
		# Changes
		# self.set_status(update=True, status=status)
		# self.update_reserved_qty()
		self.notify_update()
		clear_doctype_notifications(self)

	# Changes
	# def update_reserved_qty(self, so_item_rows=None):
	# 	"""update requested qty (before ordered_qty is updated)"""
	# 	item_wh_list = []
	# 	def _valid_for_reserve(item_code, warehouse):
	# 		if item_code and warehouse and [item_code, warehouse] not in item_wh_list \
	# 			and frappe.db.get_value("Item", item_code, "is_stock_item"):
	# 				item_wh_list.append([item_code, warehouse])

	# 	for d in self.get("items"):
	# 		if (not so_item_rows or d.name in so_item_rows) and not d.delivered_by_supplier:
	# 			if self.has_product_bundle(d.item_code):
	# 				for p in self.get("packed_items"):
	# 					if p.parent_detail_docname == d.name and p.parent_item == d.item_code:
	# 						_valid_for_reserve(p.item_code, p.warehouse)
	# 			else:
	# 				_valid_for_reserve(d.item_code, d.warehouse)

	# 	for item_code, warehouse in item_wh_list:
	# 		update_bin_qty(item_code, warehouse, {
	# 			"reserved_qty": get_reserved_qty(item_code, warehouse)
	# 		})

	def before_update_after_submit(self):
		self.validate_po()
		self.validate_drop_ship()
		self.validate_supplier_after_submit()

	def validate_supplier_after_submit(self):
		"""Check that supplier is the same after submit if PO is already made"""
		exc_list = []

		for item in self.items:
			if item.supplier:
				supplier = frappe.db.get_value("Shipping Document Item", {"parent": self.name, "item_code": item.item_code},
					"supplier")
				if item.ordered_qty > 0.0 and item.supplier != supplier:
					exc_list.append(_("Row #{0}: Not allowed to change Supplier as Purchase Order already exists").format(item.idx))

		if exc_list:
			frappe.throw('\n'.join(exc_list))

	# def update_delivery_status(self):
	# 	"""Update delivery status from Purchase Order for drop shipping"""
	# 	tot_qty, delivered_qty = 0.0, 0.0

	# 	for item in self.items:
	# 		if item.delivered_by_supplier:
	# 			item_delivered_qty  = frappe.db.sql("""select sum(qty)
	# 				from `tabPurchase Order Item` poi, `tabPurchase Order` po
	# 				where poi.sales_order_item = %s
	# 					and poi.item_code = %s
	# 					and poi.parent = po.name
	# 					and po.docstatus = 1
	# 					and po.status = 'Delivered'""", (item.name, item.item_code))

	# 			item_delivered_qty = item_delivered_qty[0][0] if item_delivered_qty else 0
	# 			item.db_set("delivered_qty", flt(item_delivered_qty), update_modified=False)

	# 		delivered_qty += item.delivered_qty
	# 		tot_qty += item.qty

	# 	self.db_set("per_delivered", flt(delivered_qty/tot_qty) * 100,
	# 		update_modified=False)

	# def set_indicator(self):
	# 	"""Set indicator for portal"""
	# 	if self.per_billed < 100 and self.per_delivered < 100:
	# 		self.indicator_color = "orange"
	# 		self.indicator_title = _("Not Paid and Not Delivered")

	# 	elif self.per_billed == 100 and self.per_delivered < 100:
	# 		self.indicator_color = "orange"
	# 		self.indicator_title = _("Paid and Not Delivered")

	# 	else:
	# 		self.indicator_color = "green"
	# 		self.indicator_title = _("Paid")

	def on_recurring(self, reference_doc):#, subscription_doc):
		self.set("delivery_date", get_next_schedule_date(reference_doc.delivery_date,
			subscription_doc.frequency, cint(subscription_doc.repeat_on_day)))

		for d in self.get("items"):
			reference_delivery_date = frappe.db.get_value("Sales Order Item",
				{"parent": reference_doc.name, "item_code": d.item_code, "idx": d.idx}, "delivery_date")

			d.set("delivery_date", get_next_schedule_date(reference_delivery_date,
				subscription_doc.frequency, cint(subscription_doc.repeat_on_day)))

@frappe.whitelist()
def make_delivery_note(source_name, target_doc=None):
	def set_missing_values(source, target):
		target.ignore_pricing_rule = 1
		target.run_method("set_missing_values")
		target.run_method("set_po_nos")
		target.run_method("calculate_taxes_and_totals")

		# set company address
		target.update(get_company_address(target.company))
		if target.company_address:
			target.update(get_fetch_values("Delivery Note", 'company_address', target.company_address))

	def update_item(source, target, source_parent):
		target.base_amount = (flt(source.qty) - flt(source.delivered_qty)) * flt(source.base_rate)
		target.amount = (flt(source.qty) - flt(source.delivered_qty)) * flt(source.rate)
		target.qty = flt(source.qty) - flt(source.delivered_qty)

		item = frappe.db.get_value("Item", target.item_code, ["item_group", "selling_cost_center"], as_dict=1)

		if item:
			target.cost_center = frappe.db.get_value("Project", source_parent.project, "cost_center") \
				or item.selling_cost_center \
				or frappe.db.get_value("Item Group", item.item_group, "default_cost_center")

	target_doc = get_mapped_doc("Shipping Document", source_name, {
		"Shipping Document": {
			"doctype": "Delivery Note",
			"validation": {
				"docstatus": ["=", 1]
			}
		},
		"Shipping Document Item": {
			"doctype": "Delivery Note Item",
			"field_map": {
				"rate": "rate",
				"so_detail": "so_detail",
				"sales_order": "against_sales_order",
			},
			"postprocess": update_item,
			"condition": lambda doc: abs(doc.delivered_qty) < abs(doc.qty) and doc.delivered_by_supplier!=1
		},
		"Sales Taxes and Charges": {
			"doctype": "Sales Taxes and Charges",
			"add_if_empty": True
		},
		"Sales Team": {
			"doctype": "Sales Team",
			"add_if_empty": True
		}
	}, target_doc, set_missing_values)

	return target_doc

@frappe.whitelist()
def make_sales_invoice(source_name, target_doc=None, ignore_permissions=False):
	def postprocess(source, target):
		set_missing_values(source, target)
		#Get the advance paid Journal Entries in Sales Invoice Advance
		target.set_advances()

	def set_missing_values(source, target):
		target.is_pos = 0
		target.ignore_pricing_rule = 1
		target.flags.ignore_permissions = True
		target.run_method("set_missing_values")
		target.run_method("set_po_nos")
		target.run_method("calculate_taxes_and_totals")

		# set company address
		target.update(get_company_address(target.company))
		if target.company_address:
			target.update(get_fetch_values("Sales Invoice", 'company_address', target.company_address))

	def update_item(source, target, source_parent):
		target.amount = flt(source.amount) - flt(source.billed_amt)
		target.base_amount = target.amount * flt(source_parent.conversion_rate)
		target.qty = target.amount / flt(source.rate) if (source.rate and source.billed_amt) else source.qty - source.returned_qty

		item = frappe.db.get_value("Item", target.item_code, ["item_group", "selling_cost_center"], as_dict=1)
		target.cost_center = frappe.db.get_value("Project", source_parent.project, "cost_center") \
			or item.selling_cost_center \
			or frappe.db.get_value("Item Group", item.item_group, "default_cost_center")

	doclist = get_mapped_doc("Shipping Document", source_name, {
		"Shipping Document": {
			"doctype": "Sales Invoice",
			"field_map": {
				"party_account_currency": "party_account_currency",
			},
			"validation": {
				"docstatus": ["=", 1]
			}
		},
		"Shipping Document Item": {
			"doctype": "Sales Invoice Item",
			"field_map": {
				"so_detail": "so_detail",
				"sales_order": "sales_order",
			},
			"postprocess": update_item,
			"condition": lambda doc: doc.qty and (doc.base_amount==0 or abs(doc.billed_amt) < abs(doc.amount))
		},
		"Sales Taxes and Charges": {
			"doctype": "Sales Taxes and Charges",
			"add_if_empty": True
		},
		"Sales Team": {
			"doctype": "Sales Team",
			"add_if_empty": True
		}
	}, target_doc, postprocess, ignore_permissions=ignore_permissions)

	return doclist


@frappe.whitelist()
def get_supplier(doctype, txt, searchfield, start, page_len, filters):
	supp_master_name = frappe.defaults.get_user_default("supp_master_name")
	if supp_master_name == "Supplier Name":
		fields = ["name", "supplier_type"]
	else:
		fields = ["name", "supplier_name", "supplier_type"]
	fields = ", ".join(fields)

	return frappe.db.sql("""select {field} from `tabSupplier`
		where docstatus < 2
			and ({key} like %(txt)s
				or supplier_name like %(txt)s)
			and name in (select supplier from `tabSales Order Item` where parent = %(parent)s)
		order by
			if(locate(%(_txt)s, name), locate(%(_txt)s, name), 99999),
			if(locate(%(_txt)s, supplier_name), locate(%(_txt)s, supplier_name), 99999),
			name, supplier_name
		limit %(start)s, %(page_len)s """.format(**{
			'field': fields,
			'key': frappe.db.escape(searchfield)
		}), {
			'txt': "%%%s%%" % txt,
			'_txt': txt.replace("%", ""),
			'start': start,
			'page_len': page_len,
			'parent': filters.get('parent')
		})

@frappe.whitelist()
def update_status(status, name):
	so = frappe.get_doc("Sales Order", name)
	so.update_status(status)
