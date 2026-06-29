#!/usr/bin/env bash
# Create a real Stripe Checkout Payment Link in TEST mode for the
# Mermelada Studio demo. Run this ONCE; the link persists in your
# Stripe account.
#
# Usage:
#   examples/mermelada-studio/setup_stripe_checkout.sh
#
# Prereqs: stripe CLI logged in, TEST mode default.

set -euo pipefail

echo "▶ Creating product 'Mermelada Studio commission'…"
PRODUCT_ID=$(stripe products create \
  -d name="Mermelada Studio commission" \
  -d description="3-slide carousel, on-brand, delivered via Hermes Agent" \
  --confirm \
  | python3 -c "import json,sys; print(json.load(sys.stdin)['id'])")
echo "  product: $PRODUCT_ID"

echo "▶ Creating \$15 price…"
PRICE_ID=$(stripe prices create \
  -d product="$PRODUCT_ID" \
  -d unit_amount=1500 \
  -d currency=usd \
  --confirm \
  | python3 -c "import json,sys; print(json.load(sys.stdin)['id'])")
echo "  price: $PRICE_ID"

echo "▶ Creating Payment Link with metadata.job_id=mermelada-commission-001…"
LINK=$(stripe payment_links create \
  -d "line_items[0][price]=$PRICE_ID" \
  -d "line_items[0][quantity]=1" \
  -d "metadata[job_id]=mermelada-commission-001" \
  -d "payment_intent_data[metadata][job_id]=mermelada-commission-001" \
  --confirm)

URL=$(echo "$LINK" | python3 -c "import json,sys; print(json.load(sys.stdin)['url'])")

echo
echo "═══════════════════════════════════════════════════════════════════"
echo "  Payment Link ready (TEST mode):"
echo
echo "    $URL"
echo
echo "  Pay it with test card 4242 4242 4242 4242 (any CVC, any future date)."
echo "  Stripe will fire payment_intent.succeeded with metadata.job_id intact."
echo "  Argus's /webhooks/stripe endpoint records the \$15 revenue row."
echo
echo "  Save the URL — it persists. Use it in the screencast:"
echo "    export ARGUS_USE_REAL_STRIPE_LINK=1"
echo "    export ARGUS_STRIPE_LINK='$URL'"
echo "    python3 examples/mermelada-studio/mermelada_demo.py"
echo "═══════════════════════════════════════════════════════════════════"
