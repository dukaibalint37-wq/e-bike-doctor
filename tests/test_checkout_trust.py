"""Stripe Checkout trust details (2026-11-22): product name + description in the buyer's language (HU/DE/EN), the official
logo as product image, a submit note (delivery by e-mail, 14-day voluntary refund, why billing data), optional payment-method
list. The billing address stays required (the invoice needs the buyer's name and address)."""
import os, unittest, urllib.parse

from tests_path import *  # noqa
import shop as S
from test_stripe33 import StripeBase


class CheckoutTrustTest(StripeBase):
    def body(self):
        return {k: v[0] for k, v in urllib.parse.parse_qs(self.stripe.calls[-1]["body"]).items()}

    def test_localized_name_description_image_and_note(self):
        os.environ["SITE_URL"] = "https://ebikedoctorpro.com"
        for lang, name, word in (("hu", "E-Bike Doctor PRO – Android PRO licenc", "e-mailben"),
                                 ("de", "E-Bike Doctor PRO – Android-PRO-Lizenz", "E-Mail"),
                                 ("en", "E-Bike Doctor PRO – Android PRO licence", "e-mailed")):
            self.shop.stripe_checkout("android", lang, True)
            q = self.body()
            self.assertEqual(q["line_items[0][price_data][product_data][name]"], name)
            self.assertIn(word, q["line_items[0][price_data][product_data][description]"])
            self.assertNotIn("web shop", q["line_items[0][price_data][product_data][name]"])
            self.assertEqual(q["line_items[0][price_data][product_data][images][0]"], "https://ebikedoctorpro.com/assets/img/logo-512.png")
            self.assertIn("14", q["custom_text[submit][message]"])
            self.assertLessEqual(len(q["custom_text[submit][message]"]), 1200)        # Stripe limit
            self.assertEqual(q["billing_address_collection"], "required")               # invoice: name + address
            self.assertEqual(q["line_items[0][price_data][unit_amount]"], "500")
        self.shop.stripe_checkout("windows", "hu", True)
        self.assertEqual(self.body()["line_items[0][price_data][product_data][name]"], "E-Bike Doctor PRO – Windows PRO licenc")

    def test_payment_methods_are_left_to_the_dashboard(self):
        # Stripe answers 400 to payment_method_types on Checkout Sessions (verified 2026-11-22): never send it
        self.shop.stripe_checkout("android", "hu", True)
        self.assertFalse([k for k in self.body() if k.startswith("payment_method_types")])

    def test_every_product_and_language_has_texts(self):
        for prod in S.PRICES:
            for lang in ("hu", "de", "en"):
                self.assertTrue(S.CHECKOUT_NAMES[prod][lang].startswith("E-Bike Doctor PRO"))
                self.assertLessEqual(len(S.CHECKOUT_DESC[prod][lang]), 500)
        for lang in ("hu", "de", "en"):
            self.assertTrue(S.CHECKOUT_SUBMIT[lang])


if __name__ == "__main__":
    unittest.main()
