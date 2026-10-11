# Research digest: external data and rules for Phase 10

Source: a web research run on 2026-10-11, condensed. Companion to `data/MANIFEST.yaml` and `docs/phases/PHASE-10-realistic-fleet-and-world.md`.

Confidence tags: **CONFIRMED** (primary source seen), **REPORTED** (secondary source), **INFERRED** (reasoning, reason given), **UNKNOWN** (not found). **The research run downloaded nothing.** Links below were seen on pages; none was fetched as a file. Verify before relying on any of it.

## 1. Which steps need external data

- Step 1 (fleet fix): rules only. Hours limits (49 CFR 395.5) and published absenteeism figures. No dataset.
- Step 2 (messy IDs): rules only. The VIN check-digit rule (49 CFR 565.15). The mess itself is invented.
- Step 3 (world engine): the scripted version needs nothing. The stochastic version needs one dataset: the NTD breakdowns table, which already holds major failures and revenue miles in the same row.
- Step 4 (shop behaviour): vocabulary from public docs; optionally one city work-order dataset for repair-duration shape. The timestamp quirks are invented.

## 2. Fleet realism

- **NTD Revenue Vehicle Inventory, report year 2024** (latest found). Landing page and a direct XLSX link (about 4.77 MB) are in the manifest. CONFIRMED that the link is listed on the page. Content: revenue vehicles by mode and type of service. Field topics (keywords, not header names): vehicle type, make, model, length, seating capacity, manufacture year, year of rebuild, ADA-accessible vehicles, ownership, funding source, fuel type, annual miles, lifetime miles. **Exact column headers: UNKNOWN.** The "2024 NTD Database File Dictionary" is linked from the landing page.
- Vehicle-type vocabulary for an enum: bus, articulated bus, cutaway, van, minivan, over-the-road bus, double-decker bus, trolleybus, school bus, automobile, SUV. CONFIRMED (keywords).
- **No VINs or plates**: rows are fleets with a vehicle count. INFERRED from the product description and the glossary; confirm when you open the file.
- NTD glossary detail worth using: year of manufacture is not model year (a model-2013 vehicle was likely made in 2012). Give mock vehicles both fields.
- Earlier vintages exist on data.transportation.gov (2023 id `na64-2g45`, 2021 id `76tr-c7h3`) with a public-domain label. CONFIRMED via the data.gov catalogue. Summary tables: Vehicles Type Count by Agency (`nimp-626k`, 97 columns, updated July 2026) and Vehicles Age Distribution (`6abt-uhgq`). CONFIRMED to exist.
- Note: the research fetcher was blocked by robots rules on `datahub.transportation.gov`. The documented Socrata API endpoints on `data.transportation.gov` should work with a normal HTTP client; unverified.

## 3. VINs, WMIs, plates

- **NHTSA vPIC API** (docs at https://vpic.nhtsa.dot.gov/api/). Endpoints: `DecodeVinValues/{VIN}?format=json`, `DecodeVin`, `DecodeVINValuesBatch` (POST), `GetWMIsForManufacturer/{name}`. No key. The official rate wording is an "automated traffic rate control mechanism" with no published number (CONFIRMED). About 50 VINs per batch call and 429s above about 5 requests per second come from third parties (REPORTED). NHTSA also offers downloadable standalone vPIC databases; the direct URL was not seen.
- **VIN check digit, 49 CFR 565.15** (eCFR link in the manifest). CONFIRMED. Letter values, position weights and the mod-11 rule are written out in the phase prompt, step 2. Position 10 is the model-year code (R=2024, S=2025, T=2026, V=2027; U, Z, 0 never used). Position 11 is the plant. For buses and incomplete vehicles the last four characters are numeric. A `9` in position 3 marks a low-volume maker whose identifier continues in positions 12 to 14. The CFR has a worked example (check digit 4); the research transcribed it as `1G4AH59H_5G118341` with the check digit position left blank. Derive the example from the CFR text yourself.
- **Candidate real WMIs**, all REPORTED from enthusiast or manufacturer pages and unchecked against vPIC: Ford incomplete vehicle `1FD` (E-Series cutaway chassis), Blue Bird `1BA` (bus) and `1BD` (incomplete), Gillig `15G`, New Flyer `5FY`, ElDorado National `1N9` with `084` in positions 12 to 14. A body-builder cutaway on a Ford chassis keeps Ford's VIN (49 CFR 565.13, CONFIRMED), a good deliberate mismatch.
- **Safe synthetic VINs (INFERRED recommendation):** default to valid but fictitious (a WMI vPIC does not recognise, correct check digit, `synthetic: true`). Use real WMIs only in an opt-in fixture. Never look synthetic VINs up in registration, recall or history services.
- **Plates (INFERRED):** formats vary by state; generate from a pattern plus a fictional jurisdiction code (`ZZ`); add dirty variants (spaces, hyphens, O versus 0) yourself.

## 4. Failure and fault calibration

- **NTD Breakdowns, 2022 to 2024** (Socrata id `amkt-4ehs`, 3,731 rows, full reporters only). CONFIRMED to exist. Columns seen in FTA's own query link: `agency, city, state, ntd_id, organization_type, reporter_type, report_year, uace_code, uza_name, primary_uza_population, agency_voms, mode, mode_name, type_of_service, mode_voms, major_mechanical_failures, other_mechanical_failures, total_mechanical_failures, vehicle_passenger_car_revenue` and several `_1` / `_2` columns (probably questionable-data flags; INFERRED). Revenue miles and major failures are in the same row, so miles between failures needs no join. Agency totals view: `fk8n-qvag`. Bus mode code `MB` and demand-response `DR` are INFERRED from standard NTD coding; check `mode_name`.
- FTA definitions (CONFIRMED): reliability is revenue miles divided by major mechanical failures. A major failure prevents a vehicle completing or starting a scheduled revenue trip; bus examples are brakes, doors, engine cooling, steering, axles, suspension.
- **The "2,700 to 12,000 miles" range was not verified.** Compute your own distribution. For context only: one agency's all-road-call figure was 2,454 to 2,613 miles in 2002 to 2003 (REPORTED, trade press), lower than a major-only figure, as expected.
- **Failures by vehicle system: no published percentages found (UNKNOWN).** Use the FTA examples above and one agency's road-call categories (air conditioning, body, wheelchair lift or ramp, dirty coach, engine, electrical, transmission, radio, tyres; REPORTED) as the category list, and set weights as an explicit assumption in `config/fault_mix.yaml`. Lead not yet opened: NREL and FTA zero-emission bus evaluation reports split road calls into propulsion versus bus-related. Ignore vendor claims such as "62% of road calls are brake, engine or electrical": no source.
- **J1939 SPN and FMI.** The authoritative list (SAE J1939 Digital Annex) is paid; commercial DBC files sell with a per-edition SAE licence (REPORTED). Open-source decoders require you to supply a licensed annex. Small openly licensed catalogues exist, for example MIT-licensed `farzadnadiri/Peykan` (SPN 190 engine speed, 513 torque, 91 accelerator position, 92 engine load, 110 coolant temperature, 174 fuel temperature, 84 vehicle speed, 183 fuel rate, 96 fuel level, 168 battery potential; DTC examples SPN 1322 FMI 31 and SPN 651 FMI 7; CONFIRMED from its README). A GPL-3.0 DBC library exists: keep it out of the repo. Most common SPN and FMI pairs on transit buses: UNKNOWN. **Recommendation:** hand-write about 15 rows in your own words; short facts such as "SPN 110 = engine coolant temperature" are fine, wholesale copying of SAE tables is not.

## 5. Maintenance-shop behaviour

- **FASTER vocabulary** (from customer-hosted public help pages; read for vocabulary only, do not scrape or redistribute): a pending repair is a repair not addressed on the current work order (item unavailable, time constraint); creating a new work order for the asset automatically adds its pending repairs; a repair on a work order can be changed to Pending; a "converted to pending count" tracks deferrals; technicians close repairs in the technician workstation before the work order can complete; creating a work order checks whether the asset status "does not allow opening of the maintenance document". CONFIRMED.
- **Fleetio API** (shape reference only; a real account is needed to call it): work orders with line items linked to issues, account-configurable work-order statuses, issues with close, resolve and reopen actions, vehicle status changes, webhooks. Issue to work order mirrors FASTER's pending repair to work order. CONFIRMED (developer docs).
- **Mock design (INFERRED):** PendingRepair(OPEN) to a foreman conversion after a delay to WorkOrder(OPEN), then repairs IN_PROGRESS, WAITING_PARTS or PENDING (deferred back), COMPLETE, work order CLOSED. An asset status can block new work orders.
- **Duration data:** Cincinnati fleet preventative maintenance and repair work orders (Socrata id `2a8x-bxjm`; description says completed work orders from January 2008 with date stamps of all milestones, costs and labour; refreshed daily). Export URL, column names and licence all UNVERIFIED. General city fleet, so use the shape (roughly lognormal open-to-close time, long parts-wait tail), not the values. Published transit-bus time-to-repair or parts-delay figures: UNKNOWN.

## 6. Operators and rules

- **49 CFR 395.5, passenger-carrying drivers** (CONFIRMED, eCFR): no driving after 10 hours following 8 consecutive hours off duty; no driving after 15 hours on duty following 8 hours off; no driving after 60 hours in 7 days or 70 hours in 8 days (the 70 hour rule applies when the carrier operates every day).
- **Public agencies are generally exempt** from the FMCSRs under 49 CFR 390.3T(f)(2), but CDL rules (Part 383) and drug and alcohol testing (Part 382) still apply. Private contractors running agency service are not covered by the government exception (REPORTED, DOT factsheet). In practice agencies use their own rules and labour agreements (INFERRED). So hours are agency configuration, defaulted from 395.5.
- **Absenteeism and extra board** (REPORTED, context not calibration): about 7% at one large agency; a 2025 study used 5%, 7.5% and 10% and found extra-board needs of 7, 10 and 13 operators in its case; large systems average about 19.6% of operators on the extra board (Transportmetrica 2025, doi 10.1080/23249935.2025.2566439, citing TCRP Report 135); a 2010 OTREC brief reports operator absenteeism over 10% against about 3.5% for the US workforce; a 1985 study found 3.5% to 5.1% (historical only); TCRP Report 169 cites 8.2% and 7.6%. POC default: 0.07 call-out probability per operator-shift, sensitivity 0.10, one to two extra-board operators in ten.
- **Qualification model (INFERRED; CDL details not re-verified against 49 CFR 383):** three checks (licence class covers the vehicle; passenger endorsement; training on the vehicle type) plus lift or ramp training for ADA-equipped vehicles, with expiry dates on training and medical cards.

## 7. Fixtures and API shapes

- **Samsara** publishes an OpenAPI (Swagger 2.0) spec at https://github.com/samsarahq/api-docs (`swagger.json`). CONFIRMED. No LICENSE file was seen, so treat as all rights reserved: reference only, hand-write your own models.
- **GTFS-Realtime:** spec pages at gtfs.org and Google's worked Service Alert example are CONFIRMED. Best practice: alert data not older than 10 minutes. Public keyless alert feeds: MBTA (REPORTED, terms unchecked) and Metrolink (CONFIRMED on the agency page, commuter rail). Use real feeds to learn wording style only; commit fictional templates.
- Geotab and other telematics DTC fields were not researched (UNKNOWN).

## 8. Identity resolution technique

- **Splink** (MIT, probabilistic linkage, DuckDB backend, v5.0.0 reported released 2026-09-28, actively maintained): overkill for about a dozen vehicles; the upgrade path.
- **recordlinkage:** last release 2023, described as unmaintained (REPORTED): avoid.
- **dedupe:** licence and 2026 status UNKNOWN.
- **rapidfuzz:** MIT, fast string similarity: use it.
- **Recommendation (INFERRED):** plain-Python rules, deterministic, explainable in the approval panel, testable with pytest. Normalise; exact VIN (validate check digit first); exact plate; fuzzy fleet number inside the agency namespace; time-bounded crosswalk; decide MATCH, AMBIGUOUS or NO_MATCH with the thresholds in the phase prompt. Keep a `Resolver` interface.

## 9. Storage and provenance rules

`data/raw/` gitignored (downloads only, never edited). `data/derived/` and `fixtures/` committed, small. Every fetch writes `PROVENANCE.json` (url, fetched_at, http_status, bytes, sha256, licence, source page); fill sha256 into `MANIFEST.lock.json` after the first fetch and fail loudly if it changes unexpectedly. Official APIs and download links only. Cache vPIC calls and keep to 1 request per second or less. Every mock response carries `"simulated": true` and a fictional product name.

## 10. Not found or not verified

The exact NTD inventory column headers and file checksum; whether any NTD product publishes per-vehicle VINs (believed not); live responses from the Socrata endpoints (the research fetcher was blocked); vPIC lookups for Gillig, New Flyer, ElDorado and Blue Bird WMIs; any percentage split of bus failures by system; the most common SPN and FMI pairs on transit buses; published transit-bus time-to-repair or parts-delay figures; Geotab DTC fields; licences for the Cincinnati data, the MBTA feed, the Samsara spec and `dedupe`; install sizes for every library; whether NTD report year 2025 files exist yet (2024 was the latest found); CDL class, endorsement and air-brake details against 49 CFR 383.
