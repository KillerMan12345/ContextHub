"""
健康指标提取器（纯规则，不调模型）
从体检报告文本中提取结构化指标 → health_metrics 表

隐私：全程本地规则，不出网关。
"""

import re
from dataclasses import dataclass
from datetime import date
from typing import Optional


@dataclass
class HealthMetric:
    person_name: str
    metric_name: str
    metric_value: float
    unit: str
    reference_range: str = ""
    is_abnormal: bool = False
    measured_date: date = None


# 常见指标 + 参考范围（简化版）
METRIC_PATTERNS = [
    # (正则, 指标名, 单位, 参考范围, 异常判断)
    (r'血压\s*(\d+)\s*/\s*(\d+)', '血压', 'mmHg', '90-140/60-90', None),
    (r'空腹血糖\s*(\d+\.?\d*)', '空腹血糖', 'mmol/L', '3.9-6.1',
     lambda v: v > 6.1 or v < 3.9),
    (r'餐后血糖\s*(\d+\.?\d*)', '餐后血糖', 'mmol/L', '<7.8',
     lambda v: v > 7.8),
    (r'总胆固醇\s*(\d+\.?\d*)', '总胆固醇', 'mmol/L', '<5.2',
     lambda v: v > 5.2),
    (r'甘油三酯\s*(\d+\.?\d*)', '甘油三酯', 'mmol/L', '<1.7',
     lambda v: v > 1.7),
    (r'高密度脂蛋白\s*(\d+\.?\d*)', '高密度脂蛋白', 'mmol/L', '>1.0',
     lambda v: v < 1.0),
    (r'低密度脂蛋白\s*(\d+\.?\d*)', '低密度脂蛋白', 'mmol/L', '<3.4',
     lambda v: v > 3.4),
    (r'糖化血红蛋白\s*(\d+\.?\d*)', '糖化血红蛋白', '%', '4.0-6.0',
     lambda v: v > 6.0),
    (r'尿酸\s*(\d+\.?\d*)', '尿酸', 'umol/L', '150-420',
     lambda v: v > 420 or v < 150),
    (r'肌酐\s*(\d+\.?\d*)', '肌酐', 'umol/L', '57-97',
     lambda v: v > 97 or v < 57),
    (r'谷丙转氨酶\s*(\d+\.?\d*)', '谷丙转氨酶', 'U/L', '9-50',
     lambda v: v > 50),
    (r'体重\s*(\d+\.?\d*)\s*(kg|公斤)', '体重', 'kg', '', None),
    (r'身高\s*(\d+\.?\d*)\s*(cm|厘米)', '身高', 'cm', '', None),
]


class HealthExtractor:
    def extract(self, text: str, person_name: str,
                measured_date: date = None) -> list[HealthMetric]:
        """从文本提取健康指标"""
        metrics = []
        measured_date = measured_date or date.today()

        for pattern, name, unit, ref_range, check_abnormal in METRIC_PATTERNS:
            for m in re.finditer(pattern, text):
                try:
                    if name == '血压':
                        # 血压特殊：收缩压/舒张压
                        sys_v = float(m.group(1))
                        dia_v = float(m.group(2))
                        abnormal = sys_v > 140 or sys_v < 90 or dia_v > 90 or dia_v < 60
                        metrics.append(HealthMetric(
                            person_name=person_name,
                            metric_name='收缩压',
                            metric_value=sys_v,
                            unit=unit,
                            reference_range='90-140',
                            is_abnormal=sys_v > 140 or sys_v < 90,
                            measured_date=measured_date,
                        ))
                        metrics.append(HealthMetric(
                            person_name=person_name,
                            metric_name='舒张压',
                            metric_value=dia_v,
                            unit=unit,
                            reference_range='60-90',
                            is_abnormal=dia_v > 90 or dia_v < 60,
                            measured_date=measured_date,
                        ))
                    else:
                        value = float(m.group(1))
                        abnormal = check_abnormal(value) if check_abnormal else False
                        metrics.append(HealthMetric(
                            person_name=person_name,
                            metric_name=name,
                            metric_value=value,
                            unit=unit,
                            reference_range=ref_range,
                            is_abnormal=abnormal,
                            measured_date=measured_date,
                        ))
                except (ValueError, IndexError):
                    continue

        return metrics

    def to_sql_values(self, metrics: list[HealthMetric],
                      space_id: str, doc_id: str) -> str:
        """生成 INSERT 语句的 VALUES 部分"""
        vals = []
        for m in metrics:
            vals.append(
                f"('{space_id}', '{doc_id}', '{m.person_name}', "
                f"'{m.metric_name}', {m.metric_value}, '{m.unit}', "
                f"'{m.reference_range}', {str(m.is_abnormal).upper()}, "
                f"'{m.measured_date}')"
            )
        return ",\n".join(vals)
