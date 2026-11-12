# Veridia Manufacturer Matcher
## Design & Engineering Note

**Project:** Manufacturer Matching Assignment  
**Technology:** Python 3.11+  
**Dataset:** `data/veridia_csv.csv`

---

### 1. Purpose and Intended Users

The Veridia Manufacturer Matcher is designed to help procurement teams and manufacturing buyers identify suitable manufacturers from a supplier dataset.

Traditionally, supplier discovery involves manual searches, spreadsheets, referrals, and repeated conversations with vendors. This process can become time-consuming and inconsistent, especially when requirements involve multiple constraints such as manufacturing processes, materials, certifications, order quantities, locations, and delivery timelines.

The tool addresses this problem by accepting a buyer's manufacturing requirement and producing a ranked, explainable shortlist of potential manufacturers.

The primary users are:

- **Procurement teams:** To identify and compare suitable suppliers efficiently.
- **Manufacturing buyers:** To find manufacturers capable of fulfilling specific technical requirements.
- **Supplier development teams:** To understand why certain manufacturers qualify or fail to qualify.
- **Operations teams:** To assess supplier availability and avoid conflicting capacity commitments.

The objective is not simply to return the highest-scoring suppliers. It is to provide a shortlist that users can understand, investigate, and act upon.

### 2. System Design and Key Engineering Choices

The application is built using Python 3.11+ and a modular architecture. Its core functionality is separated into dedicated modules for data processing, requirements, matching, scoring, explanations, and service interfaces.

The manufacturer dataset is maintained in CSV format, allowing the system to work with structured supplier information without requiring complex database infrastructure for this prototype.

The matching process follows a sequence:

1. **Requirement interpretation:** Convert the buyer's request into structured requirements, including material, manufacturing process, quantity, certification, and delivery timeline.
2. **Data validation:** Check manufacturer records for missing, inconsistent, or contradictory information.
3. **Eligibility filtering:** Remove manufacturers that fail essential requirements.
4. **Scoring and ranking:** Evaluate eligible manufacturers against relevant criteria and generate an ordered shortlist.
5. **Explanation:** Provide reasons supporting a match, together with warnings, limitations, and information about why other suppliers were excluded.

A deliberate distinction is maintained between eligibility and preference. Mandatory requirements should not be compensated for by a high score in an unrelated category. For example, a manufacturer without a required certification should not become eligible simply because it has a convenient location or a high production score.

The `matching.py`, `scoring.py`, and `explain.py` modules separate these responsibilities, making the system easier to test and maintain.

### 3. Handling Certificate Expiry and Time-Based Decisions

Manufacturing certifications are time-sensitive. A manufacturer holding ISO 9001 certification today may not necessarily hold a valid certification at a later date.

To avoid inconsistent results, the system uses an explicit reference date for time-dependent evaluation.

For reproducible evaluation, the reference date is set to **1 December 2026**, using the `--as-of` parameter.

For example:

`python -m veridia match --requirement my.json --as-of 2026-12-01`

The same requirement evaluated against the same manufacturer data and reference date should produce the same eligibility and ranking results.

This approach prevents results from changing unpredictably simply because the application is executed on a different day.

An expired certification should not satisfy a mandatory certification requirement. Where certificate information is missing or cannot be verified, the system should communicate that uncertainty rather than silently treating the certificate as valid.

### 4. Handling Two Buyers Competing for Manufacturer Capacity

A manufacturer may appear suitable for multiple buyers, but its production capacity is limited.

Consider the two scenarios specified in the assignment:

- **Buyer A:** Approximately 1,000 aluminium CNC parts, ISO 9001 certification, delivery within six weeks.
- **Buyer B:** Approximately 1,500 stainless-steel sheet-metal parts, ISO 9001 certification, with a similar delivery timeline.

If both requests arrive simultaneously, independently matching each buyer against the same original capacity figure could lead to overcommitment.

The application includes a dedicated `holds.py` module and documents an end-to-end two-buyer capacity scenario.

The intended approach is to account for existing commitments when evaluating available capacity. A capacity hold represents a provisional allocation, and subsequent requests must consider capacity already held for other buyers.

If a manufacturer cannot accommodate both requests, the system should avoid promising unavailable capacity. It should instead identify alternative manufacturers or communicate that the requested capacity cannot currently be confirmed.

An important current limitation is that sessions and holds live in memory within one process. A restart or a second worker can lose this state. Production deployment would therefore require a shared persistent store and atomic capacity-reservation operations.

This distinction between a potential match and a confirmed capacity commitment is essential for a reliable procurement workflow.

### 5. Deliberate Scope Limitations

Several features have intentionally been kept outside the scope of this prototype.

- **No automatic purchase orders:** Supplier selection does not automatically create commercial commitments.
- **No autonomous supplier verification:** Certification and manufacturer information depend on the supplied dataset and should be independently verified before procurement.
- **No complex ERP integration:** The prototype focuses on demonstrating matching and decision support rather than integrating with enterprise procurement infrastructure.
- **No unrestricted AI decision-making:** Core eligibility and ranking should remain explainable and reproducible. Language-model functionality is optional and should not override mandatory constraints.
- **No assumption of unlimited supplier capacity:** Capacity must be treated as a constrained resource rather than a static descriptive field.

The manufacturer dataset is mock data. Scores represent relative ranking, not calibrated probabilities of successful delivery or supplier performance.

These exclusions keep the prototype focused on its primary objective while avoiding unsupported claims about real-world supplier availability.

### 6. Measuring Success and Reliability

The system should be evaluated on more than whether it produces a shortlist.

The following indicators provide a practical evaluation framework:

| Evaluation area | Success indicator |
|---|---|
| Matching accuracy | Eligible manufacturers are correctly identified |
| Certification validity | Expired certifications are rejected against the reference date |
| Explainability | Each result provides understandable reasons |
| Data quality | Missing and inconsistent fields are identified |
| Capacity management | Competing requests do not exceed available capacity |
| Reproducibility | Identical inputs and reference dates produce identical results |
| Reliability | Automated tests cover important edge cases |

The repository includes dedicated test modules for matching, requirements, data handling, interfaces, and concurrency.

The automated test suite was executed locally using Python 3.11, and all **100 tests passed** in approximately 8.7 seconds.

### 7. Defending the Design Choices

A reasonable concern is whether a CSV-based system and a relatively lightweight Python architecture are sufficient for a manufacturing procurement problem.

For a take-home prototype, the objective is to demonstrate sound engineering decisions without introducing unnecessary infrastructure.

A structured CSV provides a transparent and inspectable dataset. Modular Python components make the business logic easier to understand, test, and extend. Explainable scoring helps users understand why a supplier is recommended instead of relying on an unexplained algorithmic output.

However, this architecture is not presented as a complete production procurement platform. A production deployment would require persistent transactional storage, stronger supplier-data verification, authenticated access, audit logs, and robust atomic capacity-reservation mechanisms to handle concurrent transactions across multiple users.

The system therefore prioritises correctness, transparency, reproducibility, and extensibility over premature infrastructure complexity.

### Conclusion

The Veridia Manufacturer Matcher is intended to make manufacturer discovery more structured, explainable, and reliable.

Its central contribution is the combination of requirement-based filtering, ranked supplier recommendations, data-quality awareness, reproducible certification evaluation, and capacity-aware decision support.

Rather than presenting supplier matching as a simple search operation, the project treats it as a constrained decision-making problem in which technical suitability, compliance, delivery feasibility, and available capacity must all be considered.

The design provides a practical foundation that can be extended into a more comprehensive procurement intelligence platform.
