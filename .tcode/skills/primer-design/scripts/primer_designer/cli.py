"""``python -m primer_designer`` 和 ``primer-design`` 的命令行入口。"""

from __future__ import annotations

import argparse
from pathlib import Path

from .cli_options import add_config_arguments, config_from_args
from .designer import PrimerDesigner
from .exceptions import PrimerDesignError
from .exporters import export_csv, export_xlsx
from .io import read_mutations_file, read_sequence_file


def build_parser() -> argparse.ArgumentParser:
    """声明命令行支持的输入文件、设计参数和输出选项。"""

    parser = argparse.ArgumentParser(
        description="定点突变与 NNK/NNS 饱和突变引物设计",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )

    io_group = parser.add_argument_group("输入与输出")
    io_group.add_argument(
        "--sequence", required=True, metavar="FILE", help="FASTA 或纯 DNA 序列文件"
    )
    io_group.add_argument(
        "--mutations", metavar="FILE", help="突变清单 TXT/CSV 文件"
    )
    io_group.add_argument(
        "--mutation",
        action="append",
        default=[],
        metavar="MUTATION",
        help="单个突变，可重复使用，例如 C231A、C231A:GCC、C267NNK",
    )
    io_group.add_argument(
        "--output", required=True, metavar="FILE", help="输出 .csv 或 .xlsx 文件"
    )
    io_group.add_argument(
        "--include-near-misses",
        action="store_true",
        help="在输出文件中同时写入未通过约束的近似候选",
    )

    add_config_arguments(parser)
    return parser


def main(argv: list[str] | None = None) -> int:
    """解析参数、执行设计、导出结果并返回进程退出码。"""

    args = build_parser().parse_args(argv)

    try:
        # --mutation 可以重复传入，并可与 --mutations 文件中的任务合并。
        mutations = list(args.mutation)
        if args.mutations:
            mutations.extend(read_mutations_file(args.mutations))
        # 把命令行参数转换为与 Python API 完全一致的配置对象。
        config = config_from_args(args)
        result = PrimerDesigner(config).design(read_sequence_file(args.sequence), mutations)
        output = Path(args.output)
        # 根据文件扩展名选择 Excel 或 CSV；其他扩展名默认按 CSV 处理。
        if output.suffix.lower() == ".xlsx":
            export_xlsx(result, output, include_near_misses=args.include_near_misses)
        else:
            export_csv(result, output, include_near_misses=args.include_near_misses)
    except (PrimerDesignError, OSError) as exc:
        print(f"错误：{exc}")
        return 1

    # 退出码 0 表示全部成功，2 表示任务完成但部分位点无合格候选。
    succeeded = sum(item.has_valid_candidate for item in result.results)
    print(f"已完成：{succeeded}/{len(result.results)} 个突变存在合格候选")
    print(f"结果文件：{output.resolve()}")
    return 0 if result.all_succeeded else 2


if __name__ == "__main__":
    raise SystemExit(main())
