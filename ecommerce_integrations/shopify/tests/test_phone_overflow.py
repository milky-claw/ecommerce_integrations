r"""yei-v1.4.5: unit tests for phone-overflow helpers.

Run with: python3 ecommerce_integrations/shopify/tests/test_phone_overflow.py

Bench-independent: mocks frappe.utils.validate_phone_number with a Python
port of the upstream regex (``r"^([0-9 +_\-,.()]){1,20}$"`` — see
frappe/utils/__init__.py) so the helper logic can be exercised without
a real bench. The two helpers live in a hand-loaded source slice of
utils.py (importing the package module would pull in
ecommerce_integration_log + shopify.constants which require a bench).
"""

import os
import re
import sys
import types
import unittest


# ── Mock frappe.utils.validate_phone_number with a Python port of upstream regex.
# Upstream regex (frappe v15+): re.match(r"^([0-9 +_\-,.()]){1,20}$", phone).
# Used by Frappe Contact / Address validation. Anything beyond 20 chars,
# letters, "ext.", etc. rejects.
_VALIDATE_RE = re.compile(r"^([0-9 +_\-,.()]){1,20}$")


def _fake_validate_phone_number(phone, throw=False):
	if phone is None:
		return False
	s = str(phone).strip()
	if not s:
		return False
	return bool(_VALIDATE_RE.match(s))


def _fake_cstr(x):
	return str(x) if x else ""


# Build a tiny synthetic module mirroring utils.py's helper-block.
# This avoids importing the real package (which pulls bench-only deps).
SHOPIFY_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UTILS_PY = os.path.join(SHOPIFY_DIR, "utils.py")
with open(UTILS_PY) as _f:
	_utils_src = _f.read()

# Extract the helper block (between the marker comment and the bottom of file).
_marker = "# yei-v1.4.5 phone-overflow helpers"
_idx = _utils_src.find(_marker)
assert _idx >= 0, "phone-overflow helper block missing from utils.py"
_helper_src = _utils_src[_idx:]

_test_module = types.ModuleType("yei_phone_helpers")
_test_module.__dict__["re"] = re
_test_module.__dict__["cstr"] = _fake_cstr
_test_module.__dict__["validate_phone_number"] = _fake_validate_phone_number
exec(compile(_helper_src, UTILS_PY, "exec"), _test_module.__dict__)

split_phone_overflow = _test_module.split_phone_overflow
compute_line2_with_overflow = _test_module.compute_line2_with_overflow


class TestSplitPhoneOverflow(unittest.TestCase):
	def test_split_phone_valid_passthrough(self):
		# Clean phone passes validator → returned unchanged, no overflow
		clean, has_overflow = split_phone_overflow("+1 415-419-8616")
		self.assertEqual(clean, "+1 415-419-8616")
		self.assertFalse(has_overflow)

	def test_split_phone_extension(self):
		# `ext. 67911` makes it fail validator → extract leading portion
		clean, has_overflow = split_phone_overflow("+1 415-419-8616 ext. 67911")
		self.assertEqual(clean, "+1 415-419-8616")
		self.assertTrue(has_overflow)

	def test_split_phone_concat(self):
		# Concatenated extra digits push string past 20-char limit
		clean, has_overflow = split_phone_overflow("+1 6026716610 99999 8031791701")
		self.assertEqual(clean, "+1 6026716610")
		self.assertTrue(has_overflow)

	def test_split_phone_unparseable(self):
		# Pure garbage with no extractable phone substring → ("", True)
		clean, has_overflow = split_phone_overflow("garbage")
		self.assertEqual(clean, "")
		self.assertTrue(has_overflow)

	def test_split_phone_empty(self):
		# None / empty → ("", False) — not an overflow, just absent
		clean1, has_overflow1 = split_phone_overflow(None)
		self.assertEqual(clean1, "")
		self.assertFalse(has_overflow1)

		clean2, has_overflow2 = split_phone_overflow("")
		self.assertEqual(clean2, "")
		self.assertFalse(has_overflow2)

	def test_split_phone_whitespace_only(self):
		# Whitespace-only → empty, no overflow
		clean, has_overflow = split_phone_overflow("   ")
		self.assertEqual(clean, "")
		self.assertFalse(has_overflow)


class TestComputeLine2WithOverflow(unittest.TestCase):
	def test_compute_line2_no_overflow(self):
		# Clean phone → returns shopify.address2 unchanged (no suffix)
		result = compute_line2_with_overflow("Apt 4B", "+1 415-419-8616")
		self.assertEqual(result, "Apt 4B")

	def test_compute_line2_no_overflow_empty_addr2(self):
		# Clean phone + no addr2 → empty
		result = compute_line2_with_overflow(None, "+1 415-419-8616")
		self.assertEqual(result, "")

	def test_compute_line2_with_overflow_empty_addr2(self):
		# Overflow + no addr2 → "Ph: <FULL raw>"
		result = compute_line2_with_overflow("", "+1 415-419-8616 ext. 67911")
		self.assertEqual(result, "Ph: +1 415-419-8616 ext. 67911")

	def test_compute_line2_with_overflow_with_addr2(self):
		# Overflow + existing addr2 → "<addr2> | Ph: <FULL raw>"
		result = compute_line2_with_overflow(
			"Suite 200", "+1 6026716610 99999 8031791701"
		)
		self.assertEqual(result, "Suite 200 | Ph: +1 6026716610 99999 8031791701")

	def test_compute_line2_idempotent_overwrite(self):
		# Calling twice with the same input → same output (no append-merging)
		first = compute_line2_with_overflow("Suite 200", "+1 415-419-8616 ext. 67911")
		second = compute_line2_with_overflow("Suite 200", "+1 415-419-8616 ext. 67911")
		self.assertEqual(first, second)
		self.assertEqual(first.count("| Ph:"), 1, "Suffix must appear at most once")

	def test_compute_line2_unparseable_still_appends_raw(self):
		# Unparseable phone (garbage) — still appended verbatim so courier sees it
		result = compute_line2_with_overflow("Apt 4B", "call after 5pm")
		self.assertEqual(result, "Apt 4B | Ph: call after 5pm")

	def test_compute_line2_no_phone_no_overflow(self):
		# No phone at all → just addr2 unchanged, no Ph: suffix
		result = compute_line2_with_overflow("Apt 4B", None)
		self.assertEqual(result, "Apt 4B")

	def test_compute_line2_strips_addr2_whitespace(self):
		# Whitespace in addr2 normalized before suffix appended
		result = compute_line2_with_overflow("  Suite 200  ", "+1 415-419-8616 ext. 67911")
		self.assertEqual(result, "Suite 200 | Ph: +1 415-419-8616 ext. 67911")


if __name__ == "__main__":
	unittest.main(verbosity=2)
