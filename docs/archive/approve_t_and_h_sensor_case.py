from src.storage import Storage

TARGET_DEVICE_NAME = "T & H Sensor"

storage = Storage('/data/core.db')
cases = storage.get_pending_review_cases(limit=100)

matches = [c for c in cases if c.get("device_name") == TARGET_DEVICE_NAME]

if len(matches) != 1:
    print(
        f"ABORT: expected exactly 1 pending case for {TARGET_DEVICE_NAME!r}, "
        f"found {len(matches)}"
    )
    if matches:
        for case in matches:
            print(case)
    else:
        print("Current pending cases:")
        for case in cases:
            print(
                case.get("case_id"),
                case.get("device_name"),
                case.get("classifier_name"),
                case.get("hypothesis_category"),
                case.get("observation_id"),
            )
    raise SystemExit(1)

case = matches[0]

print("About to approve:")
print("case_id:", case["case_id"])
print("device_id:", case["device_id"])
print("device_name:", case["device_name"])
print("classifier_name:", case["classifier_name"])
print("hypothesis_category:", case["hypothesis_category"])
print("observation_id:", case["observation_id"])

result = storage.resolve_review_case(
    case["case_id"],
    expected_observation_id=case["observation_id"],
    decision="approved",
)

print("resolve_result:", result)
print("pending_after:", storage.count_pending_reviews())
