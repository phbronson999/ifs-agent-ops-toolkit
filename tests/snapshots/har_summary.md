HAR file contained 13 total requests.
Found 7 distinct IFS API operation(s) across 1 projection service(s). Skipped 4 non-IFS or failed requests.

### CustomerOrderHandling.svc
- **GET CustomerOrder_Default** (1 call)
- **GET Reference_UserAllowedSiteLov** (1 call)
  - Fields: Contract, Description
  - Filter: `Contract eq '{value}'`
- **GET Reference_CustOrdCust7** (2 calls)
  - Fields: CustomerNo, Name, Company
  - Filter: `CustomerNo eq '{value}'`
  - Filter: `contains(Name, '{value}')`
- **GET CustomerOrderSet** (2 calls)
  - Fields: OrderNo
  - Filter: `DateEntered gt {date} and LineCount gt {number}`
  - Filter: `Objstate eq IfsApp.CustomerOrderHandling.CustomerOrderState'Released'`
- **POST CustomerOrderSet** (1 call)
  - Body keys: Contract, CustomerNo, OrderId, WantedDeliveryDate
- **POST CustomerOrder_ValidateOrder** [action] (1 call)
  - Body keys: RentalDb
- **PATCH CustomerOrderLineSet** (1 call)
  - Body keys: BuyQtyDue
