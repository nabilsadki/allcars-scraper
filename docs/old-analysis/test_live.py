"""Test live : 1 page de liste + 1 fiche détail, et dump JSON."""
import json, sys
sys.path.insert(0, "/mnt/4TB/scraper-avito")
from scrapper.sites.avito_http import AvitoClient

c = AvitoClient(delay=1.0, jitter=0.5)
page = c.listing_page("maroc/voitures-à_vendre", offset=0)
print(f"total catalogue : {page['total']} | annonces page 1 : {len(page['ads'])}")
a = [x for x in page["ads"] if x["price"]][0]
print("exemple :", a["title"][:50], "|", a["price_str"], "|", a["year"], a["mileage_km"], a["gearbox"], a["fuel"])
print("  images:", a["nb_images"], "| 1er full_hd:", (a["images"][0]["url_full_hd"] if a["images"] else "-"))
d = c.detail(a)
print("détail :", d["title"][:40], "| tel:", d["phone"], "| desc:", len(d["description"] or ""), "caractères")
json.dump({"listing": page["ads"][:3], "detail": d}, open("data/test_live.json", "w", encoding="utf-8"), ensure_ascii=False, indent=2)
print("dump -> data/test_live.json")
