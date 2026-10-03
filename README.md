# Lowball

Local sketch of a Florida listing board. It is not published and it is not a live feed.

`index.html` is one self-contained page. Open that file. It does not call a server.

Fetched Saturday, Oct 3, 2026 (America/New_York). Every price, cut, day count, status, and remark on a card was read from that listing's public page at fetch time.

## How many listings

24 verified listings.

- 22 from Redfin listing pages
- 2 from Zillow listing pages (4003 Booth Pl, Sarasota; 45 Lake Julia Dr S, Ponte Vedra Beach)

23 are houses, townhomes, or condos. Lake Seneca Rd, Eustis is vacant land. The page labels it Land.

One listing is Pending (Wesley Chapel). One is Contingent (Orange Park). Sarasota is under contract and accepting backups. Ponte Vedra Beach did not print a status word on the fetched page. The rest were for sale.

No listing photo URL survived the fetch, so every card uses a gray placeholder. No stock photo was added.

## Score

Total is capped at 100. A category is scored only when that listing's page shows it. Cards list the matched reasons in plain words, with the points.

Points are rounded half up to one decimal. The percent used in the math is the unrounded figure. The cut line shows that percent to one decimal.

1. Latest price cut, up to 25. No cut is 0. Points = cut percent × 2.3, then cap at 25. A 10% cut is 23. About 10.9% or more is 25. The latest cut is the most recent decrease on the current MLS number, not the drop from the first ask.
2. Number of cuts on the current MLS number: 5 each, maximum 15. A price increase is not a cut. Cuts on an older sold listing are not counted.
3. Days on market, up to 10. Points = days / 12, so about 120 days is 10. Uses the page's "days on market" line when it prints one. Otherwise uses "days on Redfin" or "days on Zillow." If the page does not print a day count, this bucket is 0. Frostproof only printed 29 hours, scored as 29/24 days.
4. Drop from the first ask on the current listing, up to 10. One point per percent off that first ask, capped at 10.
5. Off the market and then back, or expired and relisted: 12. A return from contingent or pending is scored as a fall-through (below), not also as this, unless the page shows a separate removal.
6. Relisted lower than the prior ask: 8 more. Skipped when the relist line has no price, or the new ask is not lower.
7. Pending or contingent, then back on the market: 12. If it happened more than once, the card says so. The points stay 12.
8. List price under the newest printed tax assessment, or under the last sale price: 6, once. A tax bill alone is not an assessment. An old assessment year is not used when a newer year does not print a value.
9. Vacant, already moved, tenant, estate, divorce, relocation, or seller-financing / rent-to-own language: 4. None of these pages earned it. "Move-in ready" was not treated as "already moved."
10. Remarks say motivated, bring offers, price improved, or similar: 4. The site's own "list price was lowered" banner does not count. The seller's text does.
11. Agent replaced, or the listing refreshed with no real change: 4. Not shown on these pages.
12. Sitting while nearby similar homes went under contract, or a cut right after an open house with no traction: 4. Not shown on these pages.

## What worked

Redfin individual listing pages loaded and included price history.

Two Zillow listing pages loaded without a login. They did not include a price history, so those two cards have no cut points.

## What was blocked or unusable

- Redfin's location autocomplete / stingray API returned HTTP 403 from CloudFront. No login was attempted.
- A Redfin Tampa "price reduced" filter URL came back as an unrelated Sugarcreek, Ohio results page. It was not used.
- Realtor.com listing page returned a block page ("This is taking longer than usual" / unblock request). No Realtor.com facts are on the board.
- Nobody logged into Zillow, Realtor, Redfin, or any other account.

Search snippets were not used as facts when the fetched page disagreed. Example: a snippet still had 4003 Booth Pl at $355,900 with a $19,100 cut. The fetched Zillow page said $334,900 and showed no cut, so the card uses $334,900 and no cut.

## Looked up, then left off

- 9210 40th St N, Pinellas Park: sold Sep 16, 2026.
- 417 Lake Dr, Delray Beach: sold Sep 29, 2026.
- 14823 SE County Road 100A, Starke: off market. The listing had been removed.
- 1264 Coral Farms Rd, Florahome: the page header said $265,000 and the latest history line said $268,500. Dropped rather than pick one.

## Not in this sketch

mentionmoney.com was not touched. `/workspace/mentions-app` was not touched. Nothing was published.
