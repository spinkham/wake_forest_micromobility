#!/usr/bin/env python3
"""CARTO Positron basemap: tile URL, attribution, and the API key.

In August 2026 CARTO began stamping an "API KEY REQUIRED" watermark diagonally
across raster tiles requested without a key. The tiles still serve -- they just
come back defaced -- so every map here appends a key.

The key is free: 5 million tile requests per calendar month (counted across
CARTO's raster and vector services), no CARTO account needed, meant for
non-commercial use -- personal projects, research, teaching, non-profits, which
is what this is. Requesting one takes about a minute and there is no approval
queue: <https://carto.com/basemaps/apikey/>.

    !!  IF YOU HOST THESE MAPS ANYWHERE ELSE, REPLACE CARTO_KEY BELOW.  !!

The key here is registered to the domain this project publishes from. A fork, a
mirror, or your own server needs its own key from the link above. CARTO asks
that a key not be shared across unrelated projects, and the 5M/month allowance
is per key -- a busy mirror would spend the budget for both of us.

Publishing the key in a public repo is deliberate, not a leak: a basemap key
rides along in every tile URL the browser requests, so it is public by
construction. It grants nothing but tiles.

A missing or wrong key is a soft failure, not a broken map: CARTO answers with
the same watermarked tile it serves for no key at all (verified -- byte-identical
response), so the map still draws.

CARTO and OpenStreetMap must be credited on every map built from these tiles;
CARTO_ATTR does that. See DATA_LICENSE.md.
"""

CARTO_KEY = "cb1_30p6_1_2cff2390ad0a0f5db3b8672e"

# Leaflet (folium) fills {s} from the subdomain list and {r} with "" or "@2x".
CARTO_POSITRON_URL = (
    "https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png?key=" + CARTO_KEY
)

# Matches the attribution folium's built-in "CartoDB positron" shorthand emitted
# before the key made that shorthand unusable, so the map's credit line is
# unchanged.
CARTO_ATTR = (
    '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>'
    ' contributors &copy; <a href="https://carto.com/attributions">CARTO</a>'
)


def contextily_positron():
    """CartoDB.Positron with the key attached, for contextily's add_basemap()
    in the static matplotlib figures."""
    import contextily as cx
    from xyzservices import TileProvider

    p = dict(cx.providers.CartoDB.Positron)
    p["url"] = p["url"] + "?key=" + CARTO_KEY
    return TileProvider(p)
