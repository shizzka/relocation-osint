# Search Volume Estimate

This note documents how many search operations the current Relocation OSINT pipeline can generate.

## Current implementation

The current v2 retriever performs **one logical search per criterion**.

Criteria are split into:

- **8 country-level criteria**;
- **12 city-level criteria**.

Country-level evidence is collected once per unique country.
City-level evidence is collected once per destination city.

Let:

- `C` = number of unique countries;
- `D` = number of destination country/city pairs.

For a completely fresh evidence run:

```text
logical_searches = 8 × C + 12 × D
```

Average per destination:

```text
average_searches_per_destination = 12 + 8 × C / D
```

## Examples

| Destinations | Unique countries | Fresh logical searches | Average per destination |
| ---: | ---: | ---: | ---: |
| 1 | 1 | 20 | 20.0 |
| 2 | 2 | 40 | 20.0 |
| 2 | 1 | 32 | 16.0 |
| 6 | 3 | 96 | 16.0 |
| 9 | 3 | 132 | 14.7 |
| 10 | 2 | 136 | 13.6 |
| 20 | 10 | 320 | 16.0 |

Another way to read it:

- one city per country → **20 searches per destination**;
- two cities per country → **16 searches per destination**;
- three cities per country → about **14.7**;
- four cities per country → **14**;
- five cities per country → **13.6**;
- many cities in one country → approaches **12** per destination.

## Search results fetched

Each search currently requests up to **6 results**.

Therefore the theoretical maximum number of candidate search-result rows before allowlist filtering and URL deduplication is:

```text
candidate_results <= 6 × logical_searches
```

Example: one country + one city:

```text
20 searches × 6 results = up to 120 candidate results
```

Actual evidence is lower because:

- unknown/denied domains are rejected;
- empty snippets are rejected;
- duplicate URLs are merged;
- the same URL may support more than one criterion.

## Three scout models do NOT triple search volume

Current retrieval is shared.

The pipeline performs:

```text
search once
→ build evidence pack
→ give the same evidence pack to all configured scout models
```

So three scout models still produce the same base search count:

```text
8 × C + 12 × D
```

The models multiply LLM-generation calls, not web-search calls.

## Judge stage

During the normal full runner, evidence is pinned for the lifetime of the run:

```text
RELOCATION_PIN_EVIDENCE=1
```

The judge reuses the same evidence pack.

Therefore a normal:

```text
fetch → judge → excel
```

run should add **zero extra searches at the judge stage**.

A separately forced `--refresh` can cause the evidence to be searched again.

## Cache effect

Default evidence TTL is 24 hours.

If the evidence pack is still valid and no refresh is requested, a repeated run may perform:

```text
0 new searches
```

for those cached country/city layers.

## Logical searches vs provider HTTP attempts

The formula above counts **logical search queries**.

Actual HTTP attempts can be larger because:

- an account/key may fail and another key is tried;
- a search provider may rate-limit;
- balanced mode can fall back to the other search provider;
- a failed pipeline run may later retry the unfinished task.

Under the currently documented `RELOCATION_SEARCH_PROVIDER=tavily` path, the expected successful case is one Tavily search call per logical query.

Tavily usage is recorded from the provider's returned `usage.credits`, so real credit consumption should be measured from stored evidence rather than assumed forever to equal one credit.

## Future independent retrieval lanes

If OSINT later uses multiple genuinely independent retrieval lanes, search volume changes substantially.

Let:

- `L` = number of independent retrieval lanes;
- `Q` = query variants per criterion.

Approximate fresh search volume becomes:

```text
searches = L × Q × (8 × C + 12 × D)
```

Examples for one country + one city:

| Retrieval design | Searches |
| --- | ---: |
| current shared retrieval, 1 query/criterion | 20 |
| 2 independent lanes | 40 |
| 3 independent lanes | 60 |
| 3 lanes × 2 query variants | 120 |

This is important for Search Gateway budgeting.

A grounded LLM call such as AltRouter `web_search=true` should be counted as a separate `web_grounding` lane unless it exposes sufficient URL/provenance metadata to qualify as real `web_retrieval`.

## Practical planning number

For current architecture, use:

```text
12–20 fresh logical searches per destination
```

depending on how many cities share a country.

For a shortlist with roughly two cities per country, a useful planning estimate is:

```text
~16 searches per destination
```

before retries/failover and with one retrieval lane.
