import logging
from typing import Any, Dict, List

logger = logging.getLogger("lt_vk_matcher.lt_agg")


def aggregate_leadstech_by_banner(
    rows: List[Dict[str, Any]],
    banner_sub_fields: List[str] = None,
) -> Dict[int, Dict[str, Any]]:
    """
    Складываем доход по объявлениям:

    banner_id извлекается из любого из полей banner_sub_fields (например ["sub4", "sub5"])

    На выходе:
      {
        198901961: {
          "banner_id": 198901961,
          "lt_revenue": 9319.68,
          "lt_clicks": 12,
          "lt_conversions": 2,
          "lt_approved": 1,
          "lt_inprogress": 1,
          "lt_rejected": 0,
          "raw_rows": [...]
        },
        ...
      }
    """
    if banner_sub_fields is None:
        banner_sub_fields = ["sub4", "sub5"]

    logger.info("Агрегация Leadstech по полям %s", banner_sub_fields)

    result: Dict[int, Dict[str, Any]] = {}

    for row in rows:
        # Пробуем извлечь banner_id из любого из указанных полей
        banner_id = None

        for field in banner_sub_fields:
            sub_value = row.get(field)
            if sub_value:
                try:
                    banner_id = int(str(sub_value))
                    break
                except (TypeError, ValueError):
                    logger.warning("Невозможно привести %s=%r к int", field, sub_value)
                    continue

        if banner_id is None:
            continue

        revenue = row.get("sumwebmaster", 0) or 0
        clicks = row.get("clicks", 0) or 0
        conversions = row.get("conversions", 0) or 0
        inprogress = row.get("inprogress", 0) or 0
        approved = row.get("approved", 0) or 0
        rejected = row.get("rejected", 0) or 0

        try:
            revenue = float(revenue)
        except (TypeError, ValueError):
            revenue = 0.0

        def _to_int(x: Any) -> int:
            try:
                return int(x or 0)
            except (TypeError, ValueError):
                return 0

        clicks = _to_int(clicks)
        conversions = _to_int(conversions)
        inprogress = _to_int(inprogress)
        approved = _to_int(approved)
        rejected = _to_int(rejected)

        if banner_id not in result:
            result[banner_id] = {
                "banner_id": banner_id,
                "lt_revenue": 0.0,
                "lt_clicks": 0,
                "lt_conversions": 0,
                "lt_inprogress": 0,
                "lt_approved": 0,
                "lt_rejected": 0,
                "raw_rows": [],
            }

        agg = result[banner_id]
        agg["lt_revenue"] += revenue
        agg["lt_clicks"] += clicks
        agg["lt_conversions"] += conversions
        agg["lt_inprogress"] += inprogress
        agg["lt_approved"] += approved
        agg["lt_rejected"] += rejected
        agg["raw_rows"].append(row)

    logger.info(
        "Сагрегировано %d объявлений (по %s) из Leadstech",
        len(result),
        banner_sub_fields,
    )
    return result
