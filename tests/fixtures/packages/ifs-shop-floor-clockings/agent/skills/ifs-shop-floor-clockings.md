# IFS Shop Floor Clockings — Query, Modify, and Add Machine Downtime

Read shop floor operation clockings, modify existing clockings, and add new machine downtime clockings in IFS Cloud.

Use the `call_protected_api` tool with the endpoints below.

## Base Path

All endpoints are relative to:
```
/main/ifsapplications/projection/v1/ShopFloorClockingsHandling.svc
```

---

## 1. Look Up Downtime Causes

Retrieve valid machine downtime cause codes for a site.

**Endpoint:**
```
/main/ifsapplications/projection/v1/ShopFloorClockingsHandling.svc/Reference_MachineDowntimeCause
```

**Method:** GET

**Filter examples:**

| Goal | Filter |
|------|--------|
| All active causes for a site | `Contract eq '100' and (Objstate ne IfsApp.ShopFloorClockingsHandling.MachineDowntimeCauseState'Blocked')` |
| Specific cause | `Contract eq '100' and DowntimeCauseId eq 'BREAKDOWN'` |

---

## 2. Look Up Work Centers

Retrieve valid work centers for a site.

**Endpoint:**
```
/main/ifsapplications/projection/v1/ShopFloorClockingsHandling.svc/Reference_WorkCenter
```

**Method:** GET

**Filter:** `Contract eq '100'`

---

## 3. Look Up Work Center Resources

Retrieve resources (machines) on a specific work center.

**Endpoint:**
```
/main/ifsapplications/projection/v1/ShopFloorClockingsHandling.svc/Reference_WorkCenterResource
```

**Method:** GET

**Filter:** `Contract eq '100' and WorkCenterNo eq 'MC-01'`

---

## 4. Validate Employee

Confirm an employee is valid for a given company before using their ID in a clocking action.

**Endpoint:**
```
/main/ifsapplications/projection/v1/ShopFloorClockingsHandling.svc/ValidateEmployee(ActualCompany='{Company}',EmployeeId='{EmployeeId}')
```

**Method:** GET

**Example:**
```
/main/ifsapplications/projection/v1/ShopFloorClockingsHandling.svc/ValidateEmployee(ActualCompany='10',EmployeeId='EMP001')
```

---

## 5. Get Site Report Mode

Fetch the reporting mode configured for a site (Employee vs. Anonymous).

**Endpoint:**
```
/main/ifsapplications/projection/v1/ShopFloorClockingsHandling.svc/GetSiteReportModeDb(Contract='{Contract}')
```

**Method:** GET

**Example:**
```
/main/ifsapplications/projection/v1/ShopFloorClockingsHandling.svc/GetSiteReportModeDb(Contract='100')
```

---

## 6. Query Clockings

Read existing shop floor clockings. Use `ShopOperClockingUtilSet` for all clocking types (labor, machine, interruption, indirect, downtime).

**Endpoint:**
```
/main/ifsapplications/projection/v1/ShopFloorClockingsHandling.svc/ShopOperClockingUtilSet
```

**Method:** GET

**Common filter patterns:**

| Goal | Filter |
|------|--------|
| All clockings for a shop order | `OrderNoKey eq 'SO12345' and ReleaseNoKey eq '1' and SequenceNoKey eq '1'` |
| By work center | `WorkCenterNo eq 'MC-01' and Contract eq '100'` |
| Downtime clockings only | `TimeTypeDb eq 'Downtime'` |
| Clockings for a date | `TransactionDate eq 2026-05-17` |
| By employee | `EmployeeId eq 'EMP001'` |
| Active (not yet stopped) clockings | `Stop eq false` |

**Useful `$select` fields:**
```
OrderNo,ReleaseNo,SequenceNo,OperationNo,ClockingSeq,TimeTypeDb,StartTime,FinishTime,
Duration,CrewSize,EmployeeId,TeamId,WorkCenterNo,ResourceId,DowntimeCauseId,
InterruptionCauseId,NoteText,TransactionDate,Contract,Company,Stop,DateCreated,
CreatedBy,DateModified,ModifiedBy
```

**Example — clockings for a shop order operation:**
```
/main/ifsapplications/projection/v1/ShopFloorClockingsHandling.svc/ShopOperClockingUtilSet?$filter=OrderNoKey eq 'SO12345' and ReleaseNoKey eq '1' and SequenceNoKey eq '1' and OperationNoKey eq 10&$select=ClockingSeq,TimeTypeDb,StartTime,FinishTime,Duration,EmployeeId,WorkCenterNo,ResourceId,DowntimeCauseId,NoteText,Stop&$orderby=ClockingSeq desc
```

**Example — most recent clocking for a resource:**
```
/main/ifsapplications/projection/v1/ShopFloorClockingsHandling.svc/ShopOperClockingUtilSet?$filter=ResourceId eq 'SB303'&$select=ClockingSeq,OrderNo,OperationNo,TimeTypeDb,StartTime,FinishTime,Duration,Stop,DowntimeCauseId&$orderby=ClockingSeq desc&$top=1
```

> **Ordering:** Always use `$orderby=ClockingSeq desc` (not `StartTime`) to get the most recent clocking. `ClockingSeq` is a global auto-incrementing sequence and is a more reliable recency indicator — `StartTime` can be edited after the fact.

**Key fields returned:**

| Field | Type | Description |
|-------|------|-------------|
| `ClockingSeq` | number | Unique clocking sequence number — needed for ModifyClocking |
| `OrderNoKey` | string | Shop order number (key field) |
| `OperationNoKey` | number | Operation number (key field) |
| `TimeTypeDb` | string | Raw DB string — actual values are `SETUP TIME`, `RUN TIME`, `INDIRECT TIME`, `DOWNTIME` (uppercase with spaces, not camelCase) |
| `StartTime` | string | Clocking start (DateTimeOffset) |
| `FinishTime` | string | Clocking finish (DateTimeOffset, null if still active) |
| `Duration` | number | Duration in minutes |
| `Stop` | boolean | `true` = clocking is complete, `false` = still active |
| `WorkCenterNo` | string | Work center |
| `ResourceId` | string | Machine/resource |
| `DowntimeCauseId` | string | Downtime cause (populated for Downtime clockings) |
| `EmployeeId` | string | Reporting employee |
| `NoteText` | string | Free-text note |

---

## 7. Modify a Clocking

Edit an existing clocking's times, crew size, cause, or note. Requires the `ClockingSeq` from the query result.

**Endpoint:**
```
/main/ifsapplications/projection/v1/ShopFloorClockingsHandling.svc/ModifyClocking
```

**Method:** POST

**Body fields:**

| Field | Type | Description | Example |
|-------|------|-------------|---------|
| `Contract` | string | Site | `"100"` |
| `Company` | string | Company code | `"10"` |
| `ClockingSeq` | number | Clocking to modify (from query) | `42` |
| `StartTime` | string | New start time (DateTimeOffset) | `"2026-05-17T07:00:00Z"` |
| `FinishTime` | string | New finish time (DateTimeOffset) | `"2026-05-17T09:30:00Z"` |
| `EmployeeId` | string | Employee ID | `"EMP001"` |
| `TeamId` | string | Team ID (if team-based, else `""`) | `""` |
| `CrewSize` | number | Number of crew members | `1` |
| `NoteText` | string | Free-text note | `"Corrected entry"` |
| `DowntimeCauseId` | string | Downtime cause (for Downtime clockings) | `"BREAKDOWN"` |
| `InterruptionCauseId` | string | Interruption cause (for Interruption clockings) | `""` |

**Example body:**
```json
{
  "Contract": "100",
  "Company": "10",
  "ClockingSeq": 42,
  "StartTime": "2026-05-17T07:00:00Z",
  "FinishTime": "2026-05-17T09:30:00Z",
  "EmployeeId": "EMP001",
  "TeamId": "",
  "CrewSize": 1,
  "NoteText": "Corrected entry",
  "DowntimeCauseId": "",
  "InterruptionCauseId": ""
}
```

> Omit or pass empty string for `DowntimeCauseId` and `InterruptionCauseId` unless the clocking type requires them.

---

## 8. Add Machine Downtime Clocking

Record a new machine downtime event for a work center resource.

**Endpoint:**
```
/main/ifsapplications/projection/v1/ShopFloorClockingsHandling.svc/AddMachineDowntimeClocking
```

**Method:** POST

**Body fields:**

| Field | Type | Description | Example |
|-------|------|-------------|---------|
| `Contract` | string | Site | `"100"` |
| `Company` | string | Company code | `"10"` |
| `WorkCenterNo` | string | Work center | `"MC-01"` |
| `ResourceId` | string | Machine/resource ID | `"PRESS-1"` |
| `DowntimeCauseId` | string | Downtime cause code | `"BREAKDOWN"` |
| `StartTime` | string | Downtime start (DateTimeOffset) | `"2026-05-17T06:00:00Z"` |
| `StopTime` | string | Downtime end (DateTimeOffset) | `"2026-05-17T08:30:00Z"` |
| `CurrentEmployeeId` | string | Reporting employee ID | `"EMP001"` |
| `CurrentTeamId` | string | Reporting team ID (or `""`) | `""` |
| `ClockingNote` | string | Free-text note | `"Hydraulic failure"` |

**Example body:**
```json
{
  "Contract": "100",
  "Company": "10",
  "WorkCenterNo": "MC-01",
  "ResourceId": "PRESS-1",
  "DowntimeCauseId": "BREAKDOWN",
  "StartTime": "2026-05-17T06:00:00Z",
  "StopTime": "2026-05-17T08:30:00Z",
  "CurrentEmployeeId": "EMP001",
  "CurrentTeamId": "",
  "ClockingNote": "Hydraulic failure on press 1"
}
```

---

## Enum Reference

| Field | Type Name | Values |
|-------|-----------|--------|
| `TimeTypeDb` | `ClockingTimeType` | Raw DB strings: `SETUP TIME`, `RUN TIME`, `INDIRECT TIME`, `DOWNTIME` (uppercase with spaces — **not** the camelCase enum names) |
| `ClockingType` | `ClockingType` | `Labor`, `Machine`, `Interruption` |
| `Objstate` (clocking) | `ShopOperClockingState` | `Active`, `Stopped` |
| `Objstate` (downtime cause) | `MachineDowntimeCauseState` | `Active`, `Hidden`, `Blocked` |
| `Objstate` (work center resource) | `WorkCenterResourceState` | `Active`, `Hidden`, `Blocked` |
| Site report mode | `OperationReportMode` | `Employee`, `Anonymous` |

---

## Recommended Workflows

### Query Clockings for a Shop Order
1. **Filter** `ShopOperClockingUtilSet` by `OrderNo` (and optionally `OperationNo`) — do **not** include `ResourceId` in this filter
2. **Review** `TimeTypeDb`, `StartTime`, `FinishTime`, `Duration`, `DowntimeCauseId`, `Stop`
3. Note `ClockingSeq` for any clocking you need to modify

> **Important:** If you first found the shop order by querying a preferred resource (e.g. `ResourceId eq 'SB303'`), do not carry that `ResourceId` filter into the follow-up clocking query for the order. Labor clockings on the same operation have `ResourceId = null` and will be silently excluded if you filter by resource.

### Modify an Existing Clocking
1. **Query clockings** to find the record — note its `ClockingSeq`, `Contract`, and `Company`
2. **POST** to `ModifyClocking` with corrected `StartTime`, `FinishTime`, and any other changed fields
3. Re-query to confirm the update

### Add a New Machine Downtime Clocking
1. **Look up downtime causes** — `Reference_MachineDowntimeCause?$filter=Contract eq '100'`
2. **Look up work center resources** — `Reference_WorkCenterResource?$filter=Contract eq '100' and WorkCenterNo eq 'MC-01'`
3. **Validate employee** — `ValidateEmployee(ActualCompany='10',EmployeeId='EMP001')`
4. **POST** to `AddMachineDowntimeClocking` with all required fields
5. **Confirm** by querying `ShopOperClockingUtilSet?$filter=WorkCenterNo eq 'MC-01' and TimeTypeDb eq 'Downtime'`

---

## API Notes

### Time Format
`StartTime`, `FinishTime`, and `StopTime` must be sent as **local server time without a timezone suffix** — e.g. `"2026-05-18T10:00:00"`. Do **not** append `Z` or any UTC offset. IFS compares the value against the database server's local clock, and a `Z`-suffixed timestamp is interpreted as if it were local time, making it appear to be in the future and triggering `WRNGSTARTTIME`. Values are stored and returned as UTC (`Z`-suffixed) in query responses — the no-suffix requirement applies only to write actions.

### TransactionDate vs StartTime
`TransactionDate` (`Edm.Date`) is the calendar date of the clocking — use this for date-range filters. `StartTime`/`FinishTime` are full timestamps.

### Company vs Contract
`Company` is the legal entity code (e.g. `"10"`); `Contract` is the site code (e.g. `"100"`). Both are required on write actions. The `Company` for a site can be retrieved from the order header or session context.

### ClockingSeq is the write key
To modify a clocking, you must know its `ClockingSeq`. Always query first and capture this value — it is not the same as `OperationNo` or any order key.
