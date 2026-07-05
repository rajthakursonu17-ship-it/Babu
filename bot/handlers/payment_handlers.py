"""Payment webhook stub — kept structured so a real gateway (Razorpay/UPI) can slot in."""
from __future__ import annotations

# Manual flow lives inside admin_handlers.give_access + user_handlers._handle_buy.
# This module is a placeholder so file structure matches the spec and future
# automatic gateway integration doesn't require a redesign.
