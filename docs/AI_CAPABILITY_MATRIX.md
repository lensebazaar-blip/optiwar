# Optiwar AI capability matrix

What the assistant can read and do at each stage of the customer journey, and
who owns everything else. The READ tool names are the ones the per-turn trace
records (`chat_gateway._turn_tools`); `tests/test_capability_matrix.py` fails
if a name here stops being recorded there.

| Stage | Read (trace tool) | AI action | Owner |
|---|---|---|---|
| Product discovery | YES (`search_products`) | NAVIGATE (offer, then yes) | AI |
| Fit / faces | YES (`READ_FACES`, gated) | FACE_SHOP_FOR / FACE_DEFAULT / FACE_CART_LINE (gated, Face Assistant OFF) | AI, confirmed by customer |
| Lens product page | YES (`LENS_PAGE_FACTS`) | propose prescription, customer confirms | AI / system |
| Prescription | YES (`LOOKUP_PRESCRIPTION`) | NO (entry help only) | AI / system |
| Photo | YES (`PHOTO_VISION`) | KET ticket with photo when unsure | AI / KET |
| Cart | YES (`LOOKUP_CART`) | NO (customer changes it on /cart) | customer |
| Checkout / payment | YES (`LOOKUP_ORDER`, unpaid checkouts) | NO | system / Razorpay |
| Order | YES (`LOOKUP_ORDER`) | NO | system / Ops |
| Shipping | YES (`LOOKUP_ORDER`, AWB + courier) | NO | Ops |
| Returned parcel / reship | YES (`LOOKUP_RESHIP_STATUS`) | NAVIGATE to My Orders | Ops |
| Return / reverse pickup | YES (`READ_RETURN`) | NO | Ops |
| Inspection | YES (`READ_RETURN`) | NO | Ops |
| Refund | YES (`READ_RETURN`, fee refund state) | NO | Ops / Razorpay |
| Support | YES (all of the above) | KET ticket / callback after a yes | KET |
| Satisfaction | — | asked once at a terminal support answer | AI |

The AI never moves money, books or cancels a pickup, changes an order, or
changes the cart except through a gated face action the customer confirmed.
Prices and totals are quoted from the page that calculates them, never added
up by the AI.

`LOOKUP_CART` was the only missing safe read: before it, the assistant could
not say what was in the cart except lens prescriptions and face lines.
