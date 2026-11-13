"""设计命令与外部引物评价命令共用的参数声明。"""

from __future__ import annotations

import argparse

from .models import DesignPreset, PrimerDesignConfig


def add_config_arguments(parser: argparse.ArgumentParser) -> None:
    """向命令行解析器添加完整的设计、热力学和 CDS 校验参数。"""

    design_group = parser.add_argument_group("引物设计参数")
    design_group.add_argument(
        "--preset",
        choices=tuple(item.value for item in DesignPreset),
        default=DesignPreset.QUIKCHANGE_II.value,
        help=(
            "设计规则：quikchange_ii 使用官方经验 Tm 和协议约束；"
            "primer3_generic 使用可配置的 Primer3 长度/Tm/GC 硬区间"
        ),
    )
    design_group.add_argument(
        "--strategy",
        choices=("overlap_complementary",),
        default="overlap_complementary",
        help="候选生成策略；当前版本仅支持完全互补重叠引物",
    )
    design_group.add_argument("--min-length", type=int, default=25, help="最小引物长度，bp；两种预设共用")
    design_group.add_argument("--opt-length", type=int, default=35, help="primer3_generic 排序目标长度，bp")
    design_group.add_argument("--max-length", type=int, default=45, help="primer3_generic 最大长度，bp")
    design_group.add_argument("--min-tm", type=float, default=65.0, help="primer3_generic 最低 Primer3 Tm，℃")
    design_group.add_argument("--opt-tm", type=float, default=70.0, help="primer3_generic 排序目标 Primer3 Tm，℃")
    design_group.add_argument("--max-tm", type=float, default=75.0, help="primer3_generic 最高 Primer3 Tm，℃")
    design_group.add_argument("--min-gc", type=float, default=40.0, help="primer3_generic 最低 GC，百分比")
    design_group.add_argument("--max-gc", type=float, default=65.0, help="primer3_generic 最高 GC，百分比")
    design_group.add_argument(
        "--center-slack",
        type=int,
        default=2,
        help="突变密码子相对几何中心允许移动的最大碱基数",
    )
    design_group.add_argument("--num-return", type=int, default=3, help="每个突变返回的主要候选数量")
    design_group.add_argument(
        "--near-miss-count", type=int, default=3, help="每个突变保留的近似候选数量"
    )
    design_group.add_argument(
        "--relaxed",
        action="store_true",
        help="宽松模式：允许超出约束的候选进入主要候选列表",
    )
    design_group.add_argument(
        "--relax-length-by",
        type=int,
        default=3,
        help="搜索近似候选时在长度上下限外额外扩展的 bp 数",
    )
    design_group.add_argument(
        "--max-hairpin-tm",
        type=float,
        default=None,
        metavar="CELSIUS",
        help="发卡结构最高允许 Tm；不传时只报告、不筛除",
    )
    design_group.add_argument(
        "--max-homodimer-tm",
        type=float,
        default=None,
        metavar="CELSIUS",
        help="同源二聚体最高允许 Tm；不传时只报告、不筛除",
    )
    design_group.add_argument(
        "--max-degenerate-variants",
        type=int,
        default=4096,
        help="单条简并引物允许展开的最大具体序列数",
    )

    quikchange_group = parser.add_argument_group("QuikChange II 参数")
    quikchange_group.add_argument(
        "--quikchange-min-tm",
        "--qc-min-tm",
        dest="quikchange_min_tm",
        type=float,
        default=78.0,
        help="最低 QuikChange 经验公式 Tm，℃；硬约束，默认 78",
    )
    quikchange_group.add_argument(
        "--quikchange-opt-tm",
        "--qc-opt-tm",
        dest="quikchange_opt_tm",
        type=float,
        default=None,
        metavar="CELSIUS",
        help="扩展规则：QuikChange 排序目标 Tm；不传时关闭",
    )
    quikchange_group.add_argument(
        "--quikchange-max-tm",
        "--qc-max-tm",
        dest="quikchange_max_tm",
        type=float,
        default=None,
        metavar="CELSIUS",
        help="扩展规则：最高 QuikChange 经验公式 Tm；不传时不设上限",
    )
    quikchange_group.add_argument(
        "--quikchange-opt-length",
        "--qc-opt-length",
        dest="quikchange_opt_length",
        type=int,
        default=None,
        metavar="BP",
        help="扩展规则：QuikChange 排序目标长度；不传时关闭",
    )
    quikchange_group.add_argument(
        "--quikchange-preferred-max-length",
        "--qc-preferred-max-length",
        dest="quikchange_preferred_max_length",
        type=int,
        default=45,
        help="优选长度上限，超过后仅提示和扣分、不直接淘汰",
    )
    quikchange_group.add_argument(
        "--quikchange-absolute-max-length",
        "--qc-absolute-max-length",
        dest="quikchange_absolute_max_length",
        type=int,
        default=60,
        help="搜索与外部评价使用的绝对长度上限，bp",
    )
    quikchange_group.add_argument(
        "--quikchange-min-flank",
        "--qc-min-flank",
        dest="quikchange_min_flank",
        type=int,
        default=9,
        help="密码子外侧最短正确匹配碱基数；9 bp 表示中央突变碱基距末端至少 10",
    )
    quikchange_group.add_argument(
        "--quikchange-preferred-min-gc",
        "--qc-preferred-min-gc",
        dest="quikchange_preferred_min_gc",
        type=float,
        default=40.0,
        help="优选 GC 下限，低于后仅提示和扣分、不直接淘汰",
    )
    quikchange_group.add_argument(
        "--quikchange-energy-temp",
        "--qc-energy-temp",
        dest="quikchange_energy_temp_c",
        type=float,
        default=68.0,
        help="估算错配双链自由能成本时使用的温度，℃",
    )

    thermo_group = parser.add_argument_group("Primer3 反应体系参数")
    thermo_group.add_argument("--mv-conc", type=float, default=50.0, help="单价离子浓度，mM")
    thermo_group.add_argument("--dv-conc", type=float, default=1.5, help="二价离子浓度，mM")
    thermo_group.add_argument("--dntp-conc", type=float, default=0.6, help="dNTP 总浓度，mM")
    thermo_group.add_argument("--dna-conc", type=float, default=50.0, help="引物浓度，nM")
    thermo_group.add_argument("--dmso-conc", type=float, default=0.0, help="DMSO 浓度，百分比")

    validation_group = parser.add_argument_group("CDS 校验参数")
    validation_group.add_argument(
        "--no-cds-validation",
        action="store_true",
        help="关闭全部 CDS 校验，仅按三联密码子坐标处理序列",
    )
    validation_group.add_argument(
        "--allow-non-atg-start", action="store_true", help="允许 CDS 不以 ATG 起始"
    )
    validation_group.add_argument(
        "--allow-missing-stop", action="store_true", help="允许 CDS 末尾没有终止密码子"
    )
    validation_group.add_argument(
        "--allow-internal-stops", action="store_true", help="允许 CDS 中间出现终止密码子"
    )


def config_from_args(args: argparse.Namespace) -> PrimerDesignConfig:
    """把两个 CLI 共用的参数转换为统一配置对象。"""

    return PrimerDesignConfig(
        preset=args.preset,
        strategy=args.strategy,
        min_length=args.min_length,
        opt_length=args.opt_length,
        max_length=args.max_length,
        min_tm=args.min_tm,
        opt_tm=args.opt_tm,
        max_tm=args.max_tm,
        min_gc=args.min_gc,
        max_gc=args.max_gc,
        quikchange_min_tm=args.quikchange_min_tm,
        quikchange_opt_tm=args.quikchange_opt_tm,
        quikchange_max_tm=args.quikchange_max_tm,
        quikchange_opt_length=args.quikchange_opt_length,
        quikchange_preferred_max_length=args.quikchange_preferred_max_length,
        quikchange_absolute_max_length=args.quikchange_absolute_max_length,
        quikchange_min_flank=args.quikchange_min_flank,
        quikchange_preferred_min_gc=args.quikchange_preferred_min_gc,
        quikchange_energy_temp_c=args.quikchange_energy_temp_c,
        mv_conc=args.mv_conc,
        dv_conc=args.dv_conc,
        dntp_conc=args.dntp_conc,
        dna_conc=args.dna_conc,
        dmso_conc=args.dmso_conc,
        center_slack=args.center_slack,
        num_return=args.num_return,
        near_miss_count=args.near_miss_count,
        strict=not args.relaxed,
        relax_length_by=args.relax_length_by,
        validate_cds=not args.no_cds_validation,
        require_start_codon=(
            not args.no_cds_validation and not args.allow_non_atg_start
        ),
        require_terminal_stop=(
            not args.no_cds_validation and not args.allow_missing_stop
        ),
        reject_internal_stops=(
            not args.no_cds_validation and not args.allow_internal_stops
        ),
        max_hairpin_tm=args.max_hairpin_tm,
        max_homodimer_tm=args.max_homodimer_tm,
        max_degenerate_variants=args.max_degenerate_variants,
    )
