# Copyright (c) 2018, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

import stripe
import frappe
from frappe import _
from frappe.integrations.utils import create_request_log


def create_stripe_subscription(gateway_controller, data):
	stripe_settings = frappe.get_doc("Stripe Settings", gateway_controller)
	stripe_settings.data = frappe._dict(data)

	stripe.api_key = stripe_settings.get_password(fieldname="secret_key", raise_exception=False)
	stripe.default_http_client = stripe.http_client.RequestsClient()
	stripe.api_version = "2025-06-30.basil"

	try:
		stripe_settings.integration_request = create_request_log(stripe_settings.data, "Host", "Stripe")

		# Fetch Subscription Plans from Subscription Doctype instead of Payment Plan
		payment_request_doc = frappe.get_doc("Payment Request", stripe_settings.data.reference_docname)

		if payment_request_doc.is_a_subscription:
			# Pull all subscription plan details for this request
			stripe_settings.payment_plans = frappe.get_all(
				"Subscription Plan Detail",
				filters={"parent": payment_request_doc.name},
				fields=["plan", "qty"]
			)

		else:
			stripe_settings.payment_plans = []

		return create_subscription_on_stripe(stripe_settings)

	except Exception:
		stripe_settings.log_error("Unable to create Stripe subscription")
		return {
			"redirect_to": frappe.redirect_to_message(
				_("Server Error"),
				_(
					"It seems that there is an issue with the server's stripe configuration. "
					"In case of failure, the amount will get refunded to your account."
				),
			),
			"status": 401,
		}


def _resolve_stripe_discounts(subscription_data):
    """
    Collect Stripe coupon IDs from child table rows.
    Coupon Required:coupon.custom_pricing_rules[].stripe_coupon_id(one per pricing rule)
    Direct Discount:ss_plan.membership_items[].stripe_coupon_id(one per item)
    Returns:
        list: [{"coupon": id}] for Stripe subscription discounts.
    """

    seen = set()
    discounts = []

    coupon_code = subscription_data.get("custom_coupon_code") or ""
    plan_name = subscription_data.get("custom_plan_name") or ""

    if coupon_code and frappe.db.exists("Coupon Code", coupon_code):
        coupon = frappe.get_doc("Coupon Code", coupon_code)

        for row in coupon.get("custom_pricing_rules") or []:
            cid = (row.get("stripe_coupon_id") or "").strip()

            if cid and cid not in seen:
                discounts.append({"coupon": cid})
                seen.add(cid)

    if (
        not discounts
        and plan_name
        and frappe.db.exists("SS-Pricing-Plans", plan_name)
    ):
        ss_plan = frappe.get_doc("SS-Pricing-Plans", plan_name)

        for mi in ss_plan.membership_items or []:
            cid = (mi.get("stripe_coupon_id") or "").strip()

            if cid and cid not in seen:
                discounts.append({"coupon": cid})
                seen.add(cid)

    return discounts

def create_subscription_on_stripe(stripe_settings):
	items = []
	item_one_time = []
	payment_request_doc = frappe.get_doc("Payment Request", stripe_settings.data.reference_docname)
	sales_invoice_doc = frappe.get_doc("Sales Invoice", payment_request_doc.reference_name)
	subscription_data = frappe.get_doc("Subscription", sales_invoice_doc.subscription)
	discount_items = _resolve_stripe_discounts(subscription_data)

	for payment_plan in stripe_settings.payment_plans:
        # ← plan fetched here inside loop
		plan = frappe.db.get_value("Subscription Plan", payment_plan.plan, ["product_price_id"], as_dict=True)
		price_obj = stripe.Price.retrieve(plan.product_price_id)
		if price_obj["type"] == "recurring":
			items.append({"price": plan.product_price_id, "quantity": payment_plan.qty if payment_plan.qty > 0 else 1})
		elif price_obj["type"] == "one_time":
			item_one_time.append({"price": plan.product_price_id, "quantity": payment_plan.qty if payment_plan.qty > 0 else 1})


	try:
		if subscription_data.status != "Cancelled":
			payer_email = stripe_settings.data.payer_email
			payer_name = stripe_settings.data.payer_name
			token_id = stripe_settings.data.stripe_token_id
			
			# --- STEP 1: Get fingerprint of the incoming card from token ---
			token_card = stripe.Token.retrieve(token_id).card
			new_fingerprint = token_card.fingerprint

			# --- STEP 2: Find or create customer by email ---
			existing_customers = stripe.Customer.list(email=payer_email, limit=1)
			if existing_customers.data:
				customer = existing_customers.data[0]
			else:
				customer = stripe.Customer.create(
					description=payer_name,
					email=payer_email
				)

			# --- STEP 3: Check if this card already exists for the customer ---
			existing_pms = stripe.PaymentMethod.list(customer=customer.id, type="card")
			matched_pm = None

			for pm in existing_pms.data:
				card = pm.card
				if card.fingerprint == new_fingerprint:
					matched_pm = pm
					break

			if matched_pm:
				# Card already exists → set as default
				stripe.Customer.modify(
					customer.id,
					invoice_settings={"default_payment_method": matched_pm.id}
				)
				selected_card_id = matched_pm.id

			else:
				# --- STEP 4: Validate card BEFORE saving (IMPORTANT FIX) ---
				setup_intent = stripe.SetupIntent.create(
					customer=customer.id,
					payment_method_data={
						"type": "card",
						"card": {"token": token_id}
					},
					payment_method_types=["card"],            # ← forces card only
					confirm=True,
					automatic_payment_methods={"enabled": False}  # ← disable redirect methods
				)

				if setup_intent.status != "succeeded":
					frappe.throw(_("Card validation failed. Please use another card."))

				# --- STEP 5: Validation succeeded → Now attach card ---
				payment_method_id = setup_intent.payment_method
				stripe.PaymentMethod.attach(
					payment_method_id,
					customer=customer.id,
				)
				stripe.Customer.modify(
					customer.id,
					invoice_settings={"default_payment_method": payment_method_id}
				)
				selected_card_id = payment_method_id

			subscription = stripe.Subscription.create(
				customer=customer,
				discounts=discount_items,
				items=items,
				add_invoice_items=item_one_time,
				billing_mode={"type": "flexible"},
				off_session=True,
				payment_behavior="error_if_incomplete",
				proration_behavior="none",
				metadata={
					"customer_id": customer.id,
					"subscription_id": subscription_data.name,
				}
			)

			if subscription.status == "active":
				stripe_settings.integration_request.db_set("status", "Completed", update_modified=False)
				stripe_settings.flags.status_changed_to = "Completed"

			else:
				stripe_settings.integration_request.db_set("status", "Failed", update_modified=False)
				frappe.log_error(f"Stripe Subscription ID {subscription.id}: Payment failed")
		else:
			stripe_settings.integration_request.db_set("status", "Failed", update_modified=False)
			frappe.log_error(f"Stripe Subscription ID {subscription_data.name}: Payment Link Expired")
			frappe.throw("Payment Link Expired")
	except Exception:
		stripe_settings.integration_request.db_set("status", "Failed", update_modified=False)
		stripe_settings.log_error("Unable to create Stripe subscription")

	return stripe_settings.finalize_request()
