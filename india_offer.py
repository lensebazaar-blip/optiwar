import os

try:
    from . import shipping_options
except ImportError:
    import shipping_options


FLAG = "IN_SHIPPING_DECLARED"


def enabled():
    return str(os.environ.get(FLAG, "")).strip() == "1"


def _standard_option():
    return next(
        option for option in shipping_options.OPTIONS
        if option["key"] == shipping_options.STANDARD
    )


def shipping_details():
    standard = _standard_option()
    return {
        "@type": "OfferShippingDetails",
        "shippingRate": {
            "@type": "MonetaryAmount",
            "value": format(standard["fee"], "g"),
            "currency": "INR",
        },
        "shippingDestination": {
            "@type": "DefinedRegion",
            "addressCountry": "IN",
        },
    }


def feed_shipping_xml():
    fee = format(_standard_option()["fee"], "g")
    return (
        "<g:shipping><g:country>IN</g:country>"
        "<g:service>Standard</g:service>"
        "<g:price>%s INR</g:price></g:shipping>" % fee
    )
