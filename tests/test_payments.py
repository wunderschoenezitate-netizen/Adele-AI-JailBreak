"""Pure-logic tests for orders, callbacks and the website callback endpoint. Run: python3 -m unittest discover tests"""
import hashlib
import hmac
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from http.client import HTTPConnection

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import access  # noqa: E402
import payment_config as cfg  # noqa: E402
import shkeeper_api  # noqa: E402

# Generated with shkeeper/services/webhook_hmac.py (commit e6a4128) for secret "test-api-key", ts 1767225600.
VECTOR_BODY = b'{"balance_fiat":"13","external_id":"AD-0123456789ABCDEF","fiat":"EUR","paid":true,"status":"PAID"}'
VECTOR_TS = "1767225600"
VECTOR_SIG = "a22ed78d0cfe4b0144e34c96ce6aa67dc2146fe9a10aee5f703d2669401b4aac"


def fake_invoice(crypto, order_id, eur):
    return "bc1qexampleaddressexampleaddress000000", "0.00012345"


def failing_invoice(crypto, order_id, eur):
    raise shkeeper_api.ShkeeperError("BTC payment gateway is unavailable")


def paid(order_id, balance="13", status="PAID", fiat="EUR", is_paid=True):
    return {"external_id": order_id, "status": status, "paid": is_paid, "fiat": fiat, "balance_fiat": balance,
            "crypto": "BTC", "transactions": []}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = access.connect(os.path.join(self.tmp.name, "t.sqlite3"))
        access.init_db(self.db)

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def order(self, channel="web", plan="90", **kw):
        return access.create_order(self.db, channel, plan, "BTC", invoice=fake_invoice, **kw)


class SignatureTests(unittest.TestCase):
    def test_vector_from_shkeeper_code(self):
        self.assertTrue(shkeeper_api.verify_signature("test-api-key", VECTOR_BODY, VECTOR_TS, VECTOR_SIG,
                                                      now=int(VECTOR_TS) + 10))

    def test_any_of_several_wallet_keys(self):
        now = int(VECTOR_TS)
        self.assertTrue(shkeeper_api.verify_any(["other-wallet-key", "test-api-key"], VECTOR_BODY, VECTOR_TS, VECTOR_SIG, now=now))
        self.assertFalse(shkeeper_api.verify_any(["a", "b"], VECTOR_BODY, VECTOR_TS, VECTOR_SIG, now=now))
        self.assertFalse(shkeeper_api.verify_any([], VECTOR_BODY, VECTOR_TS, VECTOR_SIG, now=now))

    def test_rejects_tampered_body_wrong_key_old_and_malformed(self):
        now = int(VECTOR_TS)
        self.assertFalse(shkeeper_api.verify_signature("test-api-key", VECTOR_BODY + b" ", VECTOR_TS, VECTOR_SIG, now=now))
        self.assertFalse(shkeeper_api.verify_signature("other", VECTOR_BODY, VECTOR_TS, VECTOR_SIG, now=now))
        self.assertFalse(shkeeper_api.verify_signature("test-api-key", VECTOR_BODY, VECTOR_TS, VECTOR_SIG, now=now + 301))
        self.assertFalse(shkeeper_api.verify_signature("test-api-key", VECTOR_BODY, "abc", VECTOR_SIG, now=now))
        self.assertFalse(shkeeper_api.verify_signature("test-api-key", VECTOR_BODY, VECTOR_TS, VECTOR_SIG[:-1], now=now))
        self.assertFalse(shkeeper_api.verify_signature("", VECTOR_BODY, VECTOR_TS, VECTOR_SIG, now=now))


class OrderTests(Base):
    def test_plan_prices(self):
        self.assertEqual(cfg.PLANS, {"30": ("5", 30), "90": ("13", 90), "365": ("48", 365)})
        o = self.order(plan="365")
        self.assertEqual((o["eur"], o["days"], o["status"]), ("48", 365, "PENDING"))
        self.assertRegex(o["order_id"], access.ORDER_ID_RE)
        self.assertEqual(access.order_by_token(self.db, o["token"])["order_id"], o["order_id"])

    def test_rejects_bad_input(self):
        with self.assertRaises(ValueError):
            access.create_order(self.db, "web", "7", "BTC", invoice=fake_invoice)
        with self.assertRaises(ValueError):
            access.create_order(self.db, "web", "30", "DOGE", invoice=fake_invoice)
        self.assertIsNone(access.order_by_token(self.db, "../../etc"))

    def test_failed_invoice_marks_order(self):
        with self.assertRaises(shkeeper_api.ShkeeperError):
            access.create_order(self.db, "web", "30", "BTC", invoice=failing_invoice)
        self.assertEqual(self.db.execute("SELECT status FROM orders").fetchone()[0], "FAILED")


class CallbackTests(Base):
    def keys(self):
        return self.db.execute("SELECT COUNT(*) FROM keys").fetchone()[0]

    def test_web_order_paid_once(self):
        o = self.order()
        self.assertEqual(access.apply_callback(self.db, paid(o["order_id"])), "paid")
        self.assertEqual(access.apply_callback(self.db, paid(o["order_id"], status="OVERPAID", balance="20")),
                         "duplicate: already paid")
        self.assertEqual(self.keys(), 1)
        after = access.order(self.db, o["order_id"])
        self.assertEqual(after["status"], "PAID")
        self.assertRegex(after["key"], access.KEY_RE)

    def test_telegram_order_extends_subscription_once(self):
        now = 1_800_000_000
        o = access.create_order(self.db, "telegram", "30", "BTC", tg_user=42, invoice=fake_invoice, now=now)
        access.apply_callback(self.db, paid(o["order_id"], balance="5"), now=now)
        access.apply_callback(self.db, paid(o["order_id"], balance="5"), now=now + 60)
        self.assertEqual(self.db.execute("SELECT expires FROM subscriptions WHERE user_id=42").fetchone()[0],
                         now + 30 * 86400)
        self.assertEqual(self.keys(), 0)
        self.assertEqual(access.pending_notifications(self.db, "tg_user"), [(o["order_id"], 42, None, 30)])
        access.mark_notified(self.db, o["order_id"])
        self.assertEqual(access.pending_notifications(self.db, "tg_user"), [])

    def test_unconfirmed_and_partial_do_not_grant(self):
        o = self.order()
        self.assertEqual(access.apply_callback(self.db, {"external_id": o["order_id"], "status": "unconfirmed"}),
                         "seen unconfirmed transaction")
        access.apply_callback(self.db, paid(o["order_id"], balance="6", status="PARTIAL", is_paid=False))
        self.assertEqual(access.order(self.db, o["order_id"])["status"], "PARTIAL")
        self.assertEqual(self.keys(), 0)
        self.assertEqual(access.apply_callback(self.db, paid(o["order_id"])), "paid")

    def test_mismatches_go_to_review(self):
        o = self.order()
        self.assertEqual(access.apply_callback(self.db, paid(o["order_id"], fiat="USD")), "review: fiat mismatch")
        o2 = self.order()
        self.assertEqual(access.apply_callback(self.db, paid(o2["order_id"], balance="1")),
                         "review: paid status below minimum ratio")
        o3 = self.order()
        self.assertEqual(access.apply_callback(self.db, paid(o3["order_id"], balance="NaN")), "review: invalid balance_fiat")
        self.assertEqual(self.keys(), 0)

    def test_late_payment_after_quote_expiry_still_grants(self):
        o = access.create_order(self.db, "web", "30", "BTC", invoice=fake_invoice, now=1000)
        self.assertEqual(access.apply_callback(self.db, paid(o["order_id"], balance="5"), now=1000 + 86400), "paid")

    def test_foreign_and_malformed(self):
        self.assertTrue(access.apply_callback(self.db, {"external_id": "107", "status": "PAID"}).startswith("ignored"))
        self.assertTrue(access.apply_callback(self.db, {"external_id": "AD-0000000000000000"}).startswith("ignored"))
        self.assertTrue(access.apply_callback(self.db, []).startswith("ignored"))

    def test_key_redeems_once(self):
        o = self.order()
        access.apply_callback(self.db, paid(o["order_id"]))
        key = access.order(self.db, o["order_id"])["key"]
        self.assertIsNone(access.redeem_key(self.db, 7, key, now=2000))
        self.assertEqual(access.redeem_key(self.db, 8, key), "❌ This key was already used.")
        self.assertEqual(self.db.execute("SELECT expires FROM subscriptions WHERE user_id=7").fetchone()[0],
                         2000 + 90 * 86400)


class ReferralTests(Base):
    NOW = 1_800_000_000

    def refer(self, referrer, uid):
        self.db.execute("INSERT OR IGNORE INTO users (user_id, referrer_id) VALUES (?, ?)", (uid, referrer))

    def qualify(self, uid, now=None):
        with self.db:
            return access.referral_qualify(self.db, uid, now or self.NOW)

    def expires(self, user):
        row = self.db.execute("SELECT expires FROM subscriptions WHERE user_id=?", (user,)).fetchone()
        return row[0] if row else None

    def test_ten_subscribed_referrals_give_one_week(self):
        self.db.execute("INSERT INTO users (user_id, referrer_id) VALUES (1, NULL)")
        for uid in range(100, 109):
            self.refer(1, uid)
            self.assertEqual(self.qualify(uid), 0)
        self.assertIsNone(self.expires(1))
        self.refer(1, 109)
        self.assertEqual(self.qualify(109), 1)
        self.assertEqual(self.expires(1), self.NOW + 7 * 86400)
        self.assertEqual(access.referral_stats(self.db, 1), (10, 10))
        self.assertEqual(access.pending_referral_rewards(self.db), [(1, 1, 7)])
        access.mark_referral_notified(self.db, 1, 1)
        self.assertEqual(access.pending_referral_rewards(self.db), [])

    def test_member_counts_once_and_one_step_per_ten(self):
        rewards = lambda: self.db.execute("SELECT COUNT(*) FROM referral_rewards").fetchone()[0]
        for uid in range(100, 120):
            self.refer(1, uid)
            self.qualify(uid)
            self.assertEqual(self.qualify(uid), 0)  # repeated call: no double count
        self.assertEqual(access.referral_stats(self.db, 1), (20, 20))
        self.assertEqual(rewards(), 2)
        self.assertEqual(self.expires(1), self.NOW + 14 * 86400)
        for uid in range(120, 129):
            self.refer(1, uid)
            self.qualify(uid)
        self.assertEqual(rewards(), 2)  # 29 members: still two steps
        self.refer(1, 129)
        self.assertEqual(self.qualify(129), 1)
        self.assertEqual(rewards(), 3)
        self.assertEqual(self.expires(1), self.NOW + 21 * 86400)

    def test_reward_extends_existing_subscription(self):
        with self.db:
            access.extend_subscription(self.db, 1, 30, self.NOW)
        for uid in range(100, 110):
            self.refer(1, uid)
            self.qualify(uid)
        self.assertEqual(self.expires(1), self.NOW + 37 * 86400)

    def test_no_referrer_self_referral_and_unknown_user_do_nothing(self):
        self.db.execute("INSERT INTO users (user_id, referrer_id) VALUES (5, NULL)")
        self.db.execute("INSERT INTO users (user_id, referrer_id) VALUES (6, 6)")
        self.assertEqual(self.qualify(5), 0)
        self.assertEqual(self.qualify(6), 0)
        self.assertEqual(self.qualify(999), 0)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM referral_qualified").fetchone()[0], 0)

    def test_redeeming_a_key_and_paying_in_telegram_both_count(self):
        self.refer(1, 50)
        o = self.order()
        access.apply_callback(self.db, paid(o["order_id"]), now=self.NOW)
        key = access.order(self.db, o["order_id"])["key"]
        self.assertIsNone(access.redeem_key(self.db, 50, key, now=self.NOW))
        self.assertEqual(access.referral_stats(self.db, 1), (1, 1))
        self.refer(1, 51)
        o2 = access.create_order(self.db, "telegram", "30", "BTC", tg_user=51, invoice=fake_invoice, now=self.NOW)
        access.apply_callback(self.db, paid(o2["order_id"], balance="5"), now=self.NOW)
        self.assertEqual(access.referral_stats(self.db, 1), (2, 2))


class WebCallbackTests(unittest.TestCase):
    """Drives the real HTTP handler on a local port."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        os.environ["DATABASE_PATH"] = os.path.join(cls.tmp.name, "w.sqlite3")
        import web
        cls.web = web
        web.DB = os.environ["DATABASE_PATH"]
        cfg.SHKEEPER_API_KEYS = ["btc-key-unrelated", "test-api-key"]
        cfg.SHKEEPER_API_KEY = cfg.SHKEEPER_API_KEYS[0]
        db = access.connect(web.DB)
        access.init_db(db)
        cls.order = access.create_order(db, "web", "90", "BTC", invoice=fake_invoice)
        db.close()
        cls.server = web.ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.tmp.cleanup()

    def post(self, body, ts, sig):
        conn = HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=5)
        conn.request("POST", cfg.CALLBACK_PATH, body, {"Content-Type": "application/json",
                                                        "X-Shkeeper-Timestamp": ts, "X-Shkeeper-Signature": sig})
        status = conn.getresponse().status
        conn.close()
        return status

    def test_signed_callback_accepted_unsigned_rejected(self):
        body = json.dumps(paid(self.order["order_id"])).encode()
        ts = str(int(time.time()))
        sig = hmac.new(b"test-api-key", ts.encode() + b"." + body, hashlib.sha256).hexdigest()
        self.assertEqual(self.post(body, ts, "0" * 64), 401)
        self.assertEqual(self.post(body, ts, sig), 202)
        self.assertEqual(self.post(body, ts, sig), 202)
        db = access.connect(self.web.DB)
        try:
            self.assertEqual(access.order(db, self.order["order_id"])["status"], "PAID")
            self.assertEqual(db.execute("SELECT COUNT(*) FROM keys").fetchone()[0], 1)
        finally:
            db.close()

    def test_pages_render(self):
        conn = HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=5)
        for path, expected in (("/impressum", 404), ("/datenschutz", 404), ("/order/" + "x" * 32, 404),
                               ("/order/" + self.order["token"], 200), ("/healthz", 200)):
            conn.request("GET", path)
            response = conn.getresponse()
            response.read()
            self.assertEqual(response.status, expected, path)
        conn.close()


if __name__ == "__main__":
    unittest.main()
