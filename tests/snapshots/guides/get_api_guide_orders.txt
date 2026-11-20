# IFS Customer Order Create — Lines & Charges

Create new customer orders, add order lines, and attach charges in IFS Cloud.

Use the `call_protected_api` tool with the endpoints below to build out new customer orders from header through lines and charges.

## Base Path

All endpoints below are relative to this base:
```
/main/ifsapplications/projection/v1/CustomerOrderHandling.svc
```

---

## 1. Get New Order Defaults

Fetch default field values for a new customer order before creating it.

**Endpoint:**
```
/main/ifsapplications/projection/v1/CustomerOrderHandling.svc/CustomerOrder_Default()
```

**Method:** GET

**Example:**
```
/main/ifsapplications/projection/v1/CustomerOrderHandling.svc/CustomerOrder_Default()
```

---

## 2. Look Up Allowed Sites

Find the sites the current user is permitted to create orders for.

**Endpoint:**
```
/main/ifsapplications/projection/v1/CustomerOrderHandling.svc/Reference_UserAllowedSiteLov
```

**Method:** GET

**Example:**
```
/main/ifsapplications/projection/v1/CustomerOrderHandling.svc/Reference_UserAllowedSiteLov?$filter=Contract eq '100'
```

---

## 3. Search for Customers

Two endpoints are available depending on search approach.

**By company and/or customer number:**
```
/main/ifsapplications/projection/v1/CustomerOrderHandling.svc/Reference_CustOrdCust6
```

| Goal | Filter |
|------|--------|
| By company | `Company eq 'COMP1'` |
| By company and customer | `Company eq 'COMP1' and CustomerNo eq 'CUST-001'` |

**By customer number only:**
```
/main/ifsapplications/projection/v1/CustomerOrderHandling.svc/Reference_CustOrdCust7
```

Filter: `CustomerNo eq 'CUST-001'`

**Method:** GET

---

## 4. Get Customer Defaults

Retrieve default values (address, payment terms, currency, etc.) for a selected customer.

**Endpoint:**
```
/main/ifsapplications/projection/v1/CustomerOrderHandling.svc/GetCustomerDefaults
```

**Method:** GET

---

## 5. Look Up Delivery and Billing Addresses

Retrieve valid ship-to and bill-to addresses for a customer.

**Method:** GET

Ship-to:
```
/main/ifsapplications/projection/v1/CustomerOrderHandling.svc/Reference_CustAddressShipLov?$filter=CustomerNo eq 'CUST-001' and AddrNo eq '1'
```

Bill-to:
```
/main/ifsapplications/projection/v1/CustomerOrderHandling.svc/Reference_CustBillAddressLov?$filter=CustomerNo eq 'CUST-001' and AddrNo eq '1'
```

---

## 6. Look Up Order Type

Retrieve valid order types (`OrderId`).

**Endpoint:**
```
/main/ifsapplications/projection/v1/CustomerOrderHandling.svc/Reference_CustOrderType
```

**Method:** GET

**Filter:** `OrderId eq 'SO'`

---

## 7. Look Up Payment Terms

**Endpoint:**
```
/main/ifsapplications/projection/v1/CustomerOrderHandling.svc/Reference_PaymentTerm
```

**Method:** GET

**Filter:** `Company eq 'COMP1' and PayTermId eq 'NET30'`

---

## 8. Look Up Currency

**Endpoint:**
```
/main/ifsapplications/projection/v1/CustomerOrderHandling.svc/Reference_IsoCurrency
```

**Method:** GET

**Filter:** `CurrencyCode eq 'USD'`

---

## 9. Look Up Ship Via

**Endpoint:**
```
/main/ifsapplications/projection/v1/CustomerOrderHandling.svc/Reference_MpccomShipVia
```

**Method:** GET

**Filter:** `ShipViaCode eq 'TRUCK'`

---

## 10. Look Up Delivery Terms

**Endpoint:**
```
/main/ifsapplications/projection/v1/CustomerOrderHandling.svc/Reference_OrderDeliveryTermLov
```

**Method:** GET

**Filter:** `DeliveryTerms eq 'EXW'`

---

## 11. Look Up Order Coordinator

Retrieve valid order coordinators, optionally excluding blocked ones.

**Endpoint:**
```
/main/ifsapplications/projection/v1/CustomerOrderHandling.svc/Reference_OrderCoordinatorLov
```

**Method:** GET

| Goal | Filter |
|------|--------|
| Active only (not blocked) | `(Objstate ne IfsApp.CustomerOrderHandling.OrderCoordinatorState'Blocked')` |
| By code | `AuthorizeCode eq 'COORD1'` |

---

## 12. Check Customer PO Number Exists

Verify a customer PO number is not already in use before creating the order.

**Endpoint:**
```
/main/ifsapplications/projection/v1/CustomerOrderHandling.svc/CheckCustPoExist
```

**Method:** GET

---

## 13. Create a New Customer Order

Create the order header. The response contains the system-assigned `OrderNo`.

**Endpoint:**
```
/main/ifsapplications/projection/v1/CustomerOrderHandling.svc/CustomerOrderSet
```

**Method:** POST

**Required body fields:**

| Field | Description | Example |
|-------|-------------|---------|
| `Contract` | Site | `"100"` |
| `CustomerNo` | Customer number | `"CUST-001"` |
| `OrderId` | Order type | `"SO"` |
| `CurrencyCode` | Currency | `"USD"` |
| `ShipAddrNo` | Ship-to address number | `"1"` |
| `BillAddrNo` | Bill-to address number | `"1"` |
| `PayTermId` | Payment terms | `"NET30"` |
| `ShipViaCode` | Ship via code | `"TRUCK"` |
| `DeliveryTerms` | Delivery terms | `"EXW"` |
| `AuthorizeCode` | Order coordinator | `"COORD1"` |

**Optional fields:**
`WantedDeliveryDate`, `CustomerPoNo`, `AgreementId`, `SalesmanCode`, `LanguageCode`, `TaxLiability`, `AddrFlag`, `BackorderOption`, `ShipmentCreation`, `IntrastatExempt`, `ShipmentType`

`WantedDeliveryDate` must be a DateTimeOffset string: `"2026-05-29T00:00:00Z"`. IFS will use this date to calculate the planned ship date for any lines added later — make sure it is far enough in the future that the planned ship date also falls on a valid future working day (typically at least 3–5 business days out to account for picking lead time).

**Example body:**
```json
{
  "Contract": "100",
  "CustomerNo": "CUST-001",
  "OrderId": "SO",
  "CurrencyCode": "USD",
  "ShipAddrNo": "1",
  "BillAddrNo": "1",
  "PayTermId": "NET30",
  "ShipViaCode": "TRUCK",
  "DeliveryTerms": "EXW",
  "AuthorizeCode": "COORD1",
  "WantedDeliveryDate": "2026-06-30T00:00:00Z",
  "CustomerPoNo": "PO-12345"
}
```

**Response shape:**
```json
{ "OrderNo": "C108200", "CustomerNo": "CUST-001", "Objstate": "Planned", ... }
```

---

## 14. Get New Order Line Defaults

Fetch default values for a new order line before adding it.

**Endpoint:**
```
/main/ifsapplications/projection/v1/CustomerOrderHandling.svc/CustomerOrderLine_Default()
```

**Method:** GET

---

## 15. Search Sales Parts

Search for available sales parts to add as order lines.

**Endpoint:**
```
/main/ifsapplications/projection/v1/CustomerOrderHandling.svc/Reference_SalesPartActiveLov
```

**Method:** GET

| Goal | Filter |
|------|--------|
| All parts for a site | `Contract eq '100'` |
| Parts starting with a prefix | `Contract eq '100' and (startswith(CatalogNo,'945'))` |
| Exact part lookup | `Contract eq '100' and CatalogNo eq '945760'` |

**Example:**
```
/main/ifsapplications/projection/v1/CustomerOrderHandling.svc/Reference_SalesPartActiveLov?$filter=Contract eq '100' and (startswith(CatalogNo,'945'))
```

---

## 16. Validate Part Number and Fetch Defaults

Validate a selected sales part and retrieve its defaults (price, UOM, cost, customer part info, etc.). Use the response fields directly when building the line save body in section 21.

**Endpoint:**
```
/main/ifsapplications/projection/v1/CustomerOrderHandling.svc/ValidateAndFetchCatalogNoDefaults
```

**Method:** POST

**Body fields:**

| Field | Type | Description | Example |
|-------|------|-------------|---------|
| `Source` | string | Always this value | `"CUSTOMERORDER"` |
| `Identity1` | string | OrderNo | `"C108200"` |
| `Identity2` | string\|null | Usually `null` for new lines | `null` |
| `Identity3` | string\|null | Usually `null` for new lines | `null` |
| `Identity4` | number | LineItemNo, always `0` | `0` |
| `CatalogNo` | string | Part/catalog number | `"945454"` |
| `BuyQtyDue` | number\|null | Requested quantity (can be `null`) | `1500` |
| `PriceEffectivityDate` | string | Date only — `YYYY-MM-DD`, no time part | `"2026-05-17"` |
| `TaxCode` | string\|null | Tax code, `null` for exempt customers | `null` |
| `ShipAddrNo` | string | Ship-to address | `"SHIP TO 2"` |
| `DeliverToCustomerNo` | string | Deliver-to customer number | `"502"` |
| `RentalDb` | string | **Must be a string**, not a boolean | `"FALSE"` |
| `RentalChargableDays` | number | Rental days (0 for non-rental) | `0` |

> **Type gotcha:** `RentalDb` is typed as `Edm.String` in the projection metadata — send `"FALSE"` or `"TRUE"`, not `false`/`true`. Sending a JSON boolean causes an `ODP_DESERIALIZATION_ERROR`. When in doubt, check the `$openapi?V2` spec (see the Authoring Guide for the debugging workflow).

**Example body:**
```json
{
  "Source": "CUSTOMERORDER",
  "Identity1": "C108200",
  "Identity2": null,
  "Identity3": null,
  "Identity4": 0,
  "CatalogNo": "945454",
  "BuyQtyDue": 1500,
  "PriceEffectivityDate": "2026-05-17",
  "TaxCode": null,
  "ShipAddrNo": "SHIP TO 2",
  "DeliverToCustomerNo": "502",
  "RentalDb": "FALSE",
  "RentalChargableDays": 0
}
```

**Key response fields to carry into the line save body:**
`CatalogNo`, `CatalogDesc`, `CatalogType`, `SalesUnitMeas`, `SaleUnitPrice`, `BaseSaleUnitPrice`, `UnitPriceInclTax`, `BaseUnitPriceInclTax`, `PartPrice`, `Cost`, `ConvFactor`, `InvertedConvFactor`, `PriceConvFactor`, `CurrencyRate`, `PriceSource`, `PriceFreeze`, `PriceSourceId`, `PriceUnitMeas`, `PartLevel`, `PartLevelId`, `CustomerLevel`, `CustomerLevelId`, `CustomerPartNo`, `CustomerPartConvFactor`, `CustomerPartUnitMeas`, `CustPartInvertConvFact`, `SupplyCode`, `TaxLiability`, `CloseTolerance`, `ChargedItem`, `PartOwnership`, `ConsignmentStock`, `CreateSmObjectOption`, `SelfBilling`, `ReceivingAdviceType`, `SupplySiteReserveType`, `BuyQtyDue`, `RevisedQtyDue`

---

## 17. Check Available Quantity

Calculate available quantity for a part before committing the line.

**Endpoint:**
```
/main/ifsapplications/projection/v1/CustomerOrderHandling.svc/CalculateAndFetchAvailableQty
```

**Method:** POST

**Body:**
```json
{
  "ValidationRec": { "OrderNo": "C108129", "LineNo": "1", "RelNo": "1", "LineItemNo": 0, "CatalogNo": "945760", "BuyQtyDue": 100, "Contract": "100" }
}
```

---

## 18. Calculate Order Line Total

Calculate pricing and totals for an order line.

**Endpoint:**
```
/main/ifsapplications/projection/v1/CustomerOrderHandling.svc/CalculateAndFetchSalesLineTotalWithHeaderParams
```

**Method:** POST

**Body fields:**
`ValidationRec`, `TaxFromDefaults`, `SupplyCountry`, `CurrencyCode`, `UsePriceInclTax`, `TaxCodeFlag`, `NewLine`, `DuplicateLine`, `TaxEdited`, `RecalcLineDisc`, `FreeOfChargeDb`, `FreeOfChgTaxPayParty`, `UseDeliveryDocAddress`, `BillAddrNo`

---

## 19. Validate Order Line Quantity

Validate the buy quantity and recalculate pricing if needed.

**Endpoint:**
```
/main/ifsapplications/projection/v1/CustomerOrderHandling.svc/ValidateBuyQtyDue
```

**Method:** POST

**Body fields:**
`ValidationRec`, `TaxFromDefaults`, `UsePriceInclTax`, `TaxCodeFlag`, `NewLine`, `DuplicateLine`, `TaxEdited`, `RecalcLineDisc`, `FreeOfChargeDb`

---

## 20. Run Credit Check

Perform a manual credit check on the order.

**Endpoint:**
```
/main/ifsapplications/projection/v1/CustomerOrderHandling.svc/ManualCreditCheck
```

**Method:** POST

**Body:**
```json
{
  "OrderNo": "C108129",
  "Info": "",
  "MessageType": "Info",
  "BlockReason": ""
}
```

---

## 21. Save the Order Line

After running all validation actions, save the line via the order's navigation link.

**Endpoint:**
```
/main/ifsapplications/projection/v1/CustomerOrderHandling.svc/CustomerOrderSet(OrderNo='{order_no}')/OrderLinesArray
```

**Method:** POST

**Required header:**
```
X-IFS-Accept-Warnings: true
```

This header is **mandatory**. Without it, IFS returns a hard error (`INVALSHIPDATE`) whenever the calculated planned ship date triggers a soft warning (e.g., order is close to or past the wanted delivery date). The UI acknowledges this warning silently; the API requires the header to do the same.

**Body:** Pass all fields returned by the validation actions as a single flat object. Numeric values must be JSON numbers (not strings). Key fields:

| Field | Type | Source | Example |
|-------|------|--------|---------|
| `OrderNo` | string | order header | `"C108200"` |
| `LineItemNo` | integer | always `0` for new lines | `0` |
| `Contract` | string | order header | `"100"` |
| `CatalogNo` | string | user input | `"945536"` |
| `PartNo` | string | = CatalogNo | `"945536"` |
| `BuyQtyDue` | number | user input | `3000` |
| `SalesUnitMeas` | string | from ValidateAndFetchCatalogNoDefaults | `"yd"` |
| `SaleUnitPrice` | number | from validation | `2.6` |
| `BaseSaleUnitPrice` | number | from validation | `2.6` |
| `UnitPriceInclTax` | number | from validation | `2.6` |
| `BaseUnitPriceInclTax` | number | from validation | `2.6` |
| `Cost` | number | from CalculateAndFetchCost | `1.8669` |
| `CatalogType` | string | from validation | `"InventoryPart"` |
| `WantedDeliveryDate` | DateTimeOffset | from order header | `"2026-06-30T00:00:00Z"` |
| `PlannedDeliveryDate` | DateTimeOffset | = WantedDeliveryDate | `"2026-06-30T00:00:00Z"` |
| `TargetDate` | DateTimeOffset | = WantedDeliveryDate | `"2026-06-30T00:00:00Z"` |
| `CustomerNo` | string | order header | `"CUST-001"` |
| `DeliverToCustomerNo` | string | = CustomerNo | `"CUST-001"` |
| `ShipAddrNo` | string | order header | `"1"` |
| `DefaultAddrFlag` | string enum | Always `"Yes"` for default ship address | `"Yes"` |
| `AddrFlag` | string enum | Always `"No"` for single-occurrence address | `"No"` |
| `PriceEffectivityDate` | date | today (`YYYY-MM-DD`) | `"2026-05-17"` |
| `IntrastatExempt` | string | from validation | `"Include"` |
| `Source` | string | always this value | `"CUSTOMERORDER"` |
| `Identity1` | string | OrderNo | `"C108200"` |
| `Identity4` | integer | always `0` | `0` |
| `FetchTaxFromDefaults` | string | always this value | `"TRUE"` |
| `Taxable` | boolean | from validation | `false` |

Additional fields to include from validation responses: `ConvFactor`, `InvertedConvFactor`, `CurrencyRate`, `PriceConvFactor`, `PriceSource`, `PriceFreeze`, `PriceSourceId`, `PartPrice`, `PriceUnitMeas`, `PartLevel`, `PartLevelId`, `CustomerLevel`, `CustomerLevelId`, `ConfigurationId`, `CustomerPartNo`, `CustomerPartConvFactor`, `CustomerPartUnitMeas`, `CustomerPartBuyQty`, `CustPartInvertConvFact`, `DesiredQty`, `RevisedQtyDue`, `SupplyCode`, `TaxLiability`, `TaxLiabilityType`, `CloseTolerance`, `ChargedItem`, `PartOwnership`, `ExchangeItem`, `SupplySiteReserveType`, `ReleasePlanning`, `SelfBilling`, `ShipmentCreation`, `ReceivingAdviceType`, `ConsignmentStock`, `CreateSmObjectOption`, `SmConnection`, `RelMtrlPlanning`, `StagedBilling`, `MultipleTaxLines`, `FreeOfCharge`, `Confirmed`, `Rental`, `RentalDb`, `Discount`, `OrderDiscount`, `DiscountAmountCurr`.

**Response shape:**
```json
{ "OrderNo": "C108200", "LineNo": "1", "RelNo": "1", "LineItemNo": 0, "Objstate": "Released", ... }
```

---

## 22. Get Charge Types

Retrieve available charge types in the "Other" charge group for a site.

**Endpoint:**
```
/main/ifsapplications/projection/v1/CustomerOrderHandling.svc/SalesChgTypeCategoryOther
```

**Method:** GET

**Filter:** `Contract eq '100'`

---

## 23. Get Charge Group and Type Description

Fetch the description for a selected charge group and type.

**Endpoint:**
```
/main/ifsapplications/projection/v1/CustomerOrderHandling.svc/GetChargeGroupAndTypeDesc
```

**Method:** GET

---

## 23. Get Charge Defaults

Retrieve default values for a selected charge type.

**Endpoint:**
```
/main/ifsapplications/projection/v1/CustomerOrderHandling.svc/GetChargeDefaults
```

**Method:** GET

---

## 24. Validate Charge Prices and Amounts

Validate charge price and amount values before saving the charge.

**Endpoint:**
```
/main/ifsapplications/projection/v1/CustomerOrderHandling.svc/ValidatePricesAndAmounts
```

**Method:** GET

---

## 25. Get Order Charge Defaults

Fetch default values for a new charge record on an existing order.

**Endpoint:**
```
/main/ifsapplications/projection/v1/CustomerOrderHandling.svc/CustomerOrderCharge_Default()
```

**Method:** GET

---

## 26. Add a Charge to an Order

Add a charge (freight, handling, etc.) via the order's charge navigation link.

**Endpoint:**
```
/main/ifsapplications/projection/v1/CustomerOrderHandling.svc/CustomerOrderSet(OrderNo='{order_no}')/CustomerOrderChargeArray
```

**Method:** POST

**Required fields:**

| Field | Type | Description | Example |
|-------|------|-------------|---------|
| `OrderNo` | string | Order number | `"C108200"` |
| `Contract` | string | Site | `"100"` |
| `Company` | string | Company code (from order header response) | `"10"` |
| `ChargeType` | string | Charge type code | `"FREIGHT"` |
| `ChargeAmount` | number | Amount excl. tax | `50` |
| `ChargeAmountInclTax` | number | Amount incl. tax | `50` |
| `BaseChargeAmount` | number | Base currency amount excl. tax | `50` |
| `BaseChargeAmtInclTax` | number | Base currency amount incl. tax | `50` |
| `ChargedQty` | number | Quantity | `1` |
| `InvoicedQty` | number | Always `0` for new charges | `0` |
| `SalesUnitMeas` | string | Unit of measure | `"EA"` |
| `IntrastatExempt` | boolean | Include in intrastat (`false` = included) | `false` |
| `Collect` | string enum | Collection method | `"Invoice"` |
| `PrintChargeType` | string enum | Print on documents | `"Yes"` |
| `PrintCollectCharge` | string enum | Print collect charge | `"NoPrint"` |
| `UnitCharge` | boolean | Per-unit charge | `false` |
| `TaxCodeFlag` | boolean | Use tax code | `false` |
| `StatisticalChargeDiff` | number | Always `0` | `0` |
| `CurrencyRate` | number | Exchange rate (1 if same currency) | `1` |
| `ChargeCost` | number | Cost of charge | `0` |
| `MultipleTaxLines` | boolean | Multiple tax lines | `false` |

**`Collect` values:** `Invoice`, `Collect`
**`PrintChargeType` values:** `Yes`, `No`
**`PrintCollectCharge` values:** `Print`, `NoPrint`

**`IntrastatExempt` note:** This is a **boolean** on charges (not the same enum as on the order header). `false` = charge is included in intrastat; `true` = exempt from intrastat.

**Example body:**
```json
{
  "OrderNo": "C108200",
  "Contract": "100",
  "Company": "10",
  "ChargeType": "FREIGHT",
  "ChargeAmount": 50,
  "ChargeAmountInclTax": 50,
  "BaseChargeAmount": 50,
  "BaseChargeAmtInclTax": 50,
  "ChargedQty": 1,
  "InvoicedQty": 0,
  "SalesUnitMeas": "EA",
  "IntrastatExempt": false,
  "Collect": "Invoice",
  "PrintChargeType": "Yes",
  "PrintCollectCharge": "NoPrint",
  "UnitCharge": false,
  "TaxCodeFlag": false,
  "StatisticalChargeDiff": 0,
  "CurrencyRate": 1,
  "ChargeCost": 0,
  "MultipleTaxLines": false
}
```

---

## Enum Reference

### CustomerOrderSet

| Field | Type Name | Values |
|-------|-----------|--------|
| `Objstate` | `CustomerOrderState` | `Planned`, `Blocked`, `Released`, `Reserved`, `Picked`, `PartiallyDelivered`, `Delivered`, `Invoiced`, `Cancelled` |
| `BackorderOption` | `CustomerBackorderOption` | `NoPartialDeliveriesAllow`, `IncompleteLinesNotAllow`, `IncompletePkgsNotAllowed`, `AllowIncompleteLinesPkgs` |
| `AddrFlag` | `GenYesNo` | `Yes`, `No` |
| `IntrastatExempt` | `IntrastatExempt` | `Include`, `Exempt` |
| `ShipmentCreation` | `ShipmentCreationCustomerOrder` | `NoAutomatic`, `OrderRelease`, `PickListCreation` |

### CustomerOrderLineSet (via OrderLinesArray)

| Field | Type Name | Values |
|-------|-----------|--------|
| `Objstate` | `CustomerOrderLineState` | `Released`, `Reserved`, `Picked`, `PartiallyDelivered`, `Delivered`, `Invoiced`, `Cancelled` |
| `CatalogType` | `SalesPartType` | `InventoryPart`, `NonInventoryPart`, `PackagePart`, `ComponentPart` |
| `PriceSource` | `PricingSource` | `Agreement`, `PriceList`, `SalesPart`, `Manual`, `Unspecified`, `PriceBreaks`, `ConditionCode`, `Campaign`, `ServicePrice`, `CostBased`, `Copied` |
| `IntrastatExempt` | `IntrastatExempt` | `Include`, `Exempt` |

### Reference_OrderCoordinatorLov

| Field | Type Name | Values |
|-------|-----------|--------|
| `Objstate` | `OrderCoordinatorState` | `Active`, `Blocked` |

### CustomerOrderChargeSet (via CustomerOrderChargeArray)

| Field | Type Name | Values |
|-------|-----------|--------|
| `Collect` | `Collect` | `Invoice`, `Collect` |
| `PrintChargeType` | `GenYesNo` | `Yes`, `No` |
| `PrintCollectCharge` | `PrintCollectCharge` | `Print`, `NoPrint` |
| `IntrastatExempt` | boolean | `true` / `false` (not an enum — different from line/order) |

---

## API Quirks

### X-IFS-Accept-Warnings header

When creating order lines, IFS may raise a soft warning (`INVALSHIPDATE`) if the calculated planned ship date falls in the past or very close to today. The API returns this as a hard error unless you include:

```
X-IFS-Accept-Warnings: true
```

The UI acknowledges this warning silently. Always include this header for order line POSTs.

### WantedDeliveryDate and planned ship date

IFS back-calculates a **planned ship date** from `WantedDeliveryDate` by subtracting picking and delivery lead times. If the result falls before today, you get `INVALSHIPDATE`. To avoid this, set `WantedDeliveryDate` far enough in the future that the planned ship date is also a valid future working day — typically at least 3–5 business days out from today.

### Navigation links vs entity sets for writes

- Lines are created via `CustomerOrderSet(OrderNo='...')/OrderLinesArray`, not directly via `CustomerOrderLineSet`
- Charges are created via `CustomerOrderSet(OrderNo='...')/CustomerOrderChargeArray`, not `CustomerOrderChargeSet`
- Reads can still use the flat entity sets (`CustomerOrderLineSet`, `CustomerOrderChargeSet`) with `$filter=OrderNo eq '...'`

### Numeric field types in POST bodies

Send all numeric values as JSON numbers, not strings (e.g., `2.6` not `"2.6"`). The browser sends strings due to `IEEE754Compatible=true` content type, but the standard JSON path accepts numbers directly.

---

## Recommended Workflows

### Create a New Customer Order

1. **Get defaults** — call `CustomerOrder_Default()` for initial field values
2. **Find site** — use `Reference_UserAllowedSiteLov`
3. **Find customer** — search `Reference_CustOrdCust6` or `Reference_CustOrdCust7`
4. **Get customer defaults** — call `GetCustomerDefaults` to prefill address, terms, currency
5. **Look up addresses** — use `Reference_CustAddressShipLov` and `Reference_CustBillAddressLov`
6. **Look up order type** — use `Reference_CustOrderType`
7. **Check PO uniqueness** — call `CheckCustPoExist` if a customer PO is provided
8. **Create the order** — POST to `CustomerOrderSet` with a `WantedDeliveryDate` at least 5 business days out; record the returned `OrderNo` and `Company`
9. **Ask about lines** — confirm with the user what lines to add before proceeding
10. **Ask about charges** — after all lines are added, always ask: *"Are there any charges to add to this order (e.g. freight, pallet, handling)?"* Charges are easy to overlook and might need to be added before the order is released.

### Add a Line to an Order

1. **Search for part** — use `Reference_SalesPartActiveLov` with `startswith` filter to confirm the `CatalogNo`
2. **Validate the part** — POST `ValidateAndFetchCatalogNoDefaults` (see section 16); the response provides `Cost`, `SaleUnitPrice`, `BaseSaleUnitPrice`, `CatalogType`, `SalesUnitMeas`, `CustomerPartNo`, and all other defaults needed for the save
3. **Check availability** — POST `CalculateAndFetchAvailableQty`
4. **Calculate line total** — POST `CalculateAndFetchSalesLineTotalWithHeaderParams`
5. **Validate quantity** — POST `ValidateBuyQtyDue`
6. **Calculate cost** — POST `CalculateAndFetchCost`
7. **Credit check** — POST `ManualCreditCheck` with the `OrderNo`
8. **Save the line** — POST to `CustomerOrderSet(OrderNo='...')/OrderLinesArray` with header `X-IFS-Accept-Warnings: true`; build the body from the `ValidateAndFetchCatalogNoDefaults` response fields plus `OrderNo`, `DefaultAddrFlag: "Yes"`, `AddrFlag: "No"`, `Source: "CUSTOMERORDER"`, `Identity1: OrderNo`, `Identity4: 0` (see section 21 for the full field list)

### Add a Charge to an Order

1. **Get charge defaults** — call `CustomerOrderCharge_Default()`
2. **Find charge type** — use `SalesChgTypeCategoryOther` filtered by `Contract`
3. **Get type description** — call `GetChargeGroupAndTypeDesc`
4. **Get charge defaults** — call `GetChargeDefaults` for the selected type
5. **Validate amounts** — call `ValidatePricesAndAmounts`
6. **Save the charge** — POST to `CustomerOrderSet(OrderNo='...')/CustomerOrderChargeArray` with all required fields including `IntrastatExempt: false` and `Company` from the order header (see section 26)

> Before fetching large datasets, add `$count=true` to check result size. If results exceed 10 records, confirm with the user before fetching all data.

For OData query syntax, use `get_api_guide({ guide: "ifs-common-odata-reference" })`.
