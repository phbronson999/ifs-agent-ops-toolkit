# IFS Customer Order Management

Query and inspect customer orders, order lines, charges, and addresses in IFS Cloud.

Use the `call_protected_api` tool with the endpoints below to retrieve customer orders and their lines.

## Base Path

All endpoints below are relative to this base:
```
/main/ifsapplications/projection/v1/CustomerOrderHandling.svc
```

---

## 1. List / Search Customer Orders

Retrieve a filtered list of customer orders.

**Endpoint:**
```
/main/ifsapplications/projection/v1/CustomerOrderHandling.svc/CustomerOrderSet
```

**Method:** GET

**Useful fields for `$select`:**
`OrderNo`, `CustomerNo`, `CustomerName`, `Objstate`, `Contract`, `CurrencyCode`, `DateEntered`, `WantedDeliveryDate`, `CustomerPoNo`, `OrderId`, `AuthorizerName`, `TotalAmount`, `BlockedType`

**OData filters:**

| Goal | Filter |
|------|--------|
| By customer number | `$filter=CustomerNo eq '1504'` |
| By order state | `$filter=Objstate eq IfsApp.CustomerOrderHandling.CustomerOrderState'Released'` |
| By customer PO | `$filter=CustomerPoNo eq 'PO-12345'` |
| By site/contract | `$filter=Contract eq '100'` |
| By date entered | `$filter=DateEntered ge 2024-01-01` |
| By currency | `$filter=CurrencyCode eq 'USD'` |
| Not blocked | `$filter=BlockedType eq IfsApp.CustomerOrderHandling.CustomerOrderBlockType'NotBlocked'` |
| Credit blocked only | `$filter=BlockedType eq IfsApp.CustomerOrderHandling.CustomerOrderBlockType'CreditBlocked'` |

**`Objstate` values (CustomerOrderState):**
`Planned`, `Blocked`, `Released`, `Reserved`, `Picked`, `PartiallyDelivered`, `Delivered`, `Invoiced`, `Cancelled`

**`BlockedType` values (CustomerOrderBlockType):**
`NotBlocked`, `CreditBlocked`, `AdvPayBlocked`, `ManualBlocked`

**Examples:**

List open released orders for a customer:
```
/main/ifsapplications/projection/v1/CustomerOrderHandling.svc/CustomerOrderSet?$filter=CustomerNo eq '1504' and Objstate eq IfsApp.CustomerOrderHandling.CustomerOrderState'Released'&$select=OrderNo,CustomerNo,CustomerName,Objstate,WantedDeliveryDate,CustomerPoNo,TotalAmount&$top=20
```

Recent orders across all customers:
```
/main/ifsapplications/projection/v1/CustomerOrderHandling.svc/CustomerOrderSet?$filter=DateEntered ge 2024-01-01&$select=OrderNo,CustomerNo,CustomerName,Objstate,DateEntered,WantedDeliveryDate,TotalAmount&$orderby=DateEntered desc&$top=20
```

All non-blocked orders:
```
/main/ifsapplications/projection/v1/CustomerOrderHandling.svc/CustomerOrderSet?$filter=BlockedType eq IfsApp.CustomerOrderHandling.CustomerOrderBlockType'NotBlocked'&$select=OrderNo,CustomerNo,CustomerName,Objstate,TotalAmount&$top=20
```

**Response shape:**
```json
{ "value": [{ "OrderNo": "C100070", "CustomerNo": "22397", "CustomerName": "...", "Objstate": "Invoiced", ... }] }
```

---

## 2. Get a Customer Order by Order Number

Retrieve a single order by its key.

**Endpoint:**
```
/main/ifsapplications/projection/v1/CustomerOrderHandling.svc/CustomerOrderSet(OrderNo='{order_no}')
```

**Method:** GET

**Example:**

Get order C100070:
```
/main/ifsapplications/projection/v1/CustomerOrderHandling.svc/CustomerOrderSet(OrderNo='C100070')
```

---

## 3. List Order Lines for an Order

Retrieve all lines belonging to a specific customer order.

**Endpoint:**
```
/main/ifsapplications/projection/v1/CustomerOrderHandling.svc/CustomerOrderLineSet
```

**Method:** GET

**Key fields:** `OrderNo`, `LineNo`, `RelNo`, `LineItemNo` (composite key)

**Useful fields for `$select`:**
`OrderNo`, `LineNo`, `RelNo`, `CatalogNo`, `CatalogDesc`, `BuyQtyDue`, `RevisedQtyDue`, `SaleUnitPrice`, `SalesUnitMeas`, `Objstate`, `PlannedDeliveryDate`, `QtyShipped`, `QtyInvoiced`, `NetAmountCurr`, `Discount`, `PartNo`, `CustomerPoNo`, `CatalogType`, `SupplyCode`, `PriceSource`

**OData filters:**

| Goal | Filter |
|------|--------|
| Lines for a specific order | `$filter=OrderNo eq 'C100070'` |
| Exclude invoiced lines | `$filter=Objstate ne IfsApp.CustomerOrderHandling.CustomerOrderLineState'Invoiced'` |
| Inventory parts only | `$filter=CatalogType eq IfsApp.CustomerOrderHandling.SalesPartType'InventoryPart'` |
| Agreement-priced lines | `$filter=PriceSource eq IfsApp.CustomerOrderHandling.PricingSource'Agreement'` |

**`Objstate` values (CustomerOrderLineState):**
`Released`, `Reserved`, `Picked`, `PartiallyDelivered`, `Delivered`, `Invoiced`, `Cancelled`

**`CatalogType` values (SalesPartType):**
`InventoryPart`, `NonInventoryPart`, `PackagePart`, `ComponentPart`

**`PriceSource` values (PricingSource):**
`Agreement`, `PriceList`, `SalesPart`, `Manual`, `Unspecified`, `PriceBreaks`, `ConditionCode`, `Campaign`, `ServicePrice`, `CostBased`, `Copied`

**Examples:**

All lines for order C100070:
```
/main/ifsapplications/projection/v1/CustomerOrderHandling.svc/CustomerOrderLineSet?$filter=OrderNo eq 'C100070'&$select=OrderNo,LineNo,RelNo,CatalogNo,CatalogDesc,BuyQtyDue,SaleUnitPrice,SalesUnitMeas,Objstate,QtyShipped,NetAmountCurr
```

Open lines not yet invoiced:
```
/main/ifsapplications/projection/v1/CustomerOrderHandling.svc/CustomerOrderLineSet?$filter=OrderNo eq 'C100070' and Objstate ne IfsApp.CustomerOrderHandling.CustomerOrderLineState'Invoiced'&$select=OrderNo,LineNo,CatalogNo,CatalogDesc,BuyQtyDue,QtyShipped,PlannedDeliveryDate
```

**Response shape:**
```json
{ "value": [{ "OrderNo": "C100070", "LineNo": "1", "RelNo": "1", "CatalogNo": "945760", "BuyQtyDue": 77440, ... }] }
```

---

## 4. Get a Specific Order Line

**Endpoint:**
```
/main/ifsapplications/projection/v1/CustomerOrderHandling.svc/CustomerOrderLineSet(OrderNo='{order_no}',LineNo='{line_no}',RelNo='{rel_no}',LineItemNo={item_no})
```

**Method:** GET

**Example:**
```
/main/ifsapplications/projection/v1/CustomerOrderHandling.svc/CustomerOrderLineSet(OrderNo='C100070',LineNo='1',RelNo='1',LineItemNo=0)
```

---

## 5. List Order Charges

Retrieve additional charges attached to a customer order (freight, handling, etc.).

**Endpoint:**
```
/main/ifsapplications/projection/v1/CustomerOrderHandling.svc/CustomerOrderChargeSet
```

**Method:** GET

**Key fields:** `OrderNo`, `SequenceNo` (composite key)

**Useful fields for `$select`:**
`OrderNo`, `SequenceNo`, `ChargeType`, `ChargeTypeDesc`, `ChargedQty`, `ChargeAmount`, `ChargeAmountInclTax`, `CurrencyCode`, `Collect`, `PrintChargeType`

**`Collect` values (Collect):**
`Invoice`, `Collect`

**Example:**

All charges for order C100070:
```
/main/ifsapplications/projection/v1/CustomerOrderHandling.svc/CustomerOrderChargeSet?$filter=OrderNo eq 'C100070'&$select=OrderNo,SequenceNo,ChargeType,ChargeTypeDesc,ChargedQty,ChargeAmount,Collect
```

---

## 6. Order Addresses

Retrieve delivery and bill-to addresses associated with an order.

**Endpoint:**
```
/main/ifsapplications/projection/v1/CustomerOrderHandling.svc/CustomerOrderAddresses
```

**Method:** GET

**Filter by order:**
```
/main/ifsapplications/projection/v1/CustomerOrderHandling.svc/CustomerOrderAddresses?$filter=OrderNo eq 'C100070'
```

---

## Enum Reference

Use the fully qualified type name when filtering on enum fields. Format: `IfsApp.CustomerOrderHandling.{TypeName}'{Value}'`

### CustomerOrderSet

| Field | Type Name | Values |
|-------|-----------|--------|
| `Objstate` | `CustomerOrderState` | `Planned`, `Blocked`, `Released`, `Reserved`, `Picked`, `PartiallyDelivered`, `Delivered`, `Invoiced`, `Cancelled` |
| `BlockedType` | `CustomerOrderBlockType` | `NotBlocked`, `CreditBlocked`, `AdvPayBlocked`, `ManualBlocked` |
| `BackorderOption` | `CustomerBackorderOption` | `NoPartialDeliveriesAllow`, `IncompleteLinesNotAllow`, `IncompletePkgsNotAllowed`, `AllowIncompleteLinesPkgs` |
| `AddrFlag` | `GenYesNo` | `Yes`, `No` |
| `IntrastatExempt` | `IntrastatExempt` | `Include`, `Exempt` |
| `ShipmentCreation` | `ShipmentCreationCustomerOrder` | `NoAutomatic`, `OrderRelease`, `PickListCreation` |
| `OrderConfFlag` | `PrintOrderConfirmation` | `PrintOrderConf`, `NoOrderConf` |
| `OrderConf` | `OrderConfirmationPrinted` | `Printed`, `NotPrinted` |

### CustomerOrderLineSet

| Field | Type Name | Values |
|-------|-----------|--------|
| `Objstate` | `CustomerOrderLineState` | `Released`, `Reserved`, `Picked`, `PartiallyDelivered`, `Delivered`, `Invoiced`, `Cancelled` |
| `CatalogType` | `SalesPartType` | `InventoryPart`, `NonInventoryPart`, `PackagePart`, `ComponentPart` |
| `PriceSource` | `PricingSource` | `Agreement`, `PriceList`, `SalesPart`, `Manual`, `Unspecified`, `PriceBreaks`, `ConditionCode`, `Campaign`, `ServicePrice`, `CostBased`, `Copied` |
| `PartOwnership` | `PartOwnershipSubset1` | `CompanyOwned`, `CustomerOwned`, `SupplierLoaned`, `CompanyRentalAsset` |
| `StagedBilling` | `StagedBillingType` | `StagedBilling`, `NotStagedBilling` |
| `CapabilityCheckResult` | `CapCheckResultType` | `ReqDateFulfilled`, `ReqDateNotFulfilled` |
| `AddrFlag` | `GenYesNo` | `Yes`, `No` |

### CustomerOrderChargeSet

| Field | Type Name | Values |
|-------|-----------|--------|
| `Collect` | `Collect` | `Invoice`, `Collect` |
| `PrintChargeType` | `GenYesNo` | `Yes`, `No` |
| `PrintCollectCharge` | `PrintCollectCharge` | `Print`, `NoPrint` |

---

## Recommended Workflows

1. **Find orders for a customer** — filter `CustomerOrderSet` by `CustomerNo`, optionally also by `Objstate`.
2. **Look up a specific order** — use the single-record endpoint with `OrderNo`.
3. **Inspect order lines** — filter `CustomerOrderLineSet` by `OrderNo` to see all lines with quantities and pricing.
4. **Check delivery status** — use `QtyShipped`, `QtyInvoiced`, `QtyAssigned`, and `Objstate` on each line.
5. **Check for blocked orders** — filter by `BlockedType ne IfsApp.CustomerOrderHandling.CustomerOrderBlockType'NotBlocked'`.
6. **Find charges on an order** — filter `CustomerOrderChargeSet` by `OrderNo`.

> Before fetching large datasets, add `$count=true` to check result size. If results exceed 10 records, confirm with the user before fetching all data.

For OData query syntax, use `get_api_guide({ guide: "ifs-common-odata-reference" })`.
