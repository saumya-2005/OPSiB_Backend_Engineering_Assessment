OPSiB BACKEND ENGINEERING ASSESSMENT - FIXTURE DATA

All data is synthetic. No real retailer or employee data is included.

Frozen evaluation time (use this rather than your machine clock):
2026-10-16T15:00:00+05:30

Files
- purchase_order_lines.csv: ERP purchase-order lines. One identical duplicate row is included.
- goods_receipt_lines.csv: WMS goods-receipt lines. One identical duplicate source record is included.
- product_aliases.csv: source-specific product and unit mappings into a canonical product/base unit.
- location_aliases.csv: ERP, WMS and HRMS location identifiers mapped to canonical locations.
- employees.csv: HRMS employee roster; employment status must be respected.
- workforce_status.csv: sampled attendance/shift status, including conflicting eligibility context.

Important semantic boundary
Regional employees may have a region scope without a store-level location. Do not force every employee into a store.

The fixtures do NOT include a supplier dispatch/ASN dataset. They support identifying a receipt shortfall against the PO after the receiving window is complete. They do NOT prove whether the supplier dispatched less than ordered, whether a carrier lost stock, or where the discrepancy occurred. Avoid asserting a root cause not supported by the data.

Expected evaluation scenarios are documented in the PDF assessment. You may add your own tests and synthetic rows. Do not alter fixture files in place for your submitted test evidence; preserve the originals and document any derived data.
