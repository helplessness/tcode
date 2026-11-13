#!/usr/bin/env python3
"""
Protein Physical & Chemical Properties Calculator
Based on VenusFactory's property calculation pipeline

Usage:
    python property_calculator.py --fasta_file input.fasta
    python property_calculator.py --pdb_file structure.pdb
"""

import os
import sys
import argparse
import json
from datetime import datetime
from Bio import SeqIO
from Bio.SeqUtils.ProtParam import ProteinAnalysis
from typing import Dict, Any, Tuple

# Try to import Bio.PDB for structure analysis
try:
    from Bio.PDB import PDBParser, DSSP, SASA

    PDB_AVAILABLE = True
except ImportError:
    PDB_AVAILABLE = False
    print("Warning: Bio.PDB not fully available. Structure analysis disabled.")


def extract_seq_from_pdb(pdb_file: str, chain_id: str = "A") -> str:
    """Extract protein sequence from PDB file."""
    parser = PDBParser(QUIET=True)
    structure = parser.get_structure("protein", pdb_file)

    sequence = []
    for model in structure:
        for chain in model:
            if chain.id == chain_id:
                for residue in chain:
                    if residue.get_resname() in [
                        "ALA",
                        "CYS",
                        "ASP",
                        "GLU",
                        "PHE",
                        "GLY",
                        "HIS",
                        "ILE",
                        "LYS",
                        "LEU",
                        "MET",
                        "ASN",
                        "PRO",
                        "GLN",
                        "ARG",
                        "SER",
                        "THR",
                        "VAL",
                        "TRP",
                        "TYR",
                    ]:
                        aa = residue.get_resname()[0]
                        sequence.append(aa)
                break

    return "".join(sequence)


def calculate_physicochemical(sequence: str) -> Dict[str, Any]:
    """Calculate basic physicochemical properties using BioPython."""
    # Clean sequence
    clean_seq = sequence.replace("*", "").replace("X", "C").replace("U", "C")

    # Create analysis object
    analysed_seq = ProteinAnalysis(clean_seq)

    # Get secondary structure fractions
    ss_fraction = analysed_seq.secondary_structure_fraction()

    properties = {
        "sequence_length": len(clean_seq),
        "molecular_weight": round(analysed_seq.molecular_weight(), 2),
        "theoretical_pI": round(analysed_seq.isoelectric_point(), 2),
        "aromaticity": round(analysed_seq.aromaticity(), 3),
        "instability_index": round(analysed_seq.instability_index(), 2),
        "gravy": round(analysed_seq.gravy(), 3),
        "secondary_structure_fraction": {
            "helix": round(ss_fraction[0], 3),
            "turn": round(ss_fraction[1], 3),
            "sheet": round(ss_fraction[2], 3),
        },
        "amino_acid_composition": {
            aa: round(count, 4)
            for aa, count in analysed_seq.get_amino_acids_percent().items()
        },
    }

    # Add stability prediction
    properties["stability_prediction"] = (
        "Stable" if properties["instability_index"] <= 40 else "Unstable"
    )

    return properties


def calculate_sasa(pdb_file: str, chain_id: str = "A") -> Dict[str, Any]:
    """Calculate Solvent Accessible Surface Area (SASA)."""
    if not PDB_AVAILABLE:
        return {"error": "Bio.PDB not available"}

    parser = PDBParser(QUIET=True)
    structure = parser.get_structure("protein", pdb_file)

    # Calculate SASA using Shrake-Rupley algorithm
    sr = SASA.ShrakeRupley()
    sr.compute(structure, level="R")  # Compute for residues

    total_sasa = 0.0
    residue_count = 0
    sasa_per_residue = []

    for model in structure:
        for chain in model:
            if chain.id == chain_id:
                for residue in chain:
                    if "SASA" in residue.xtra:
                        sasa = residue.xtra["SASA"]
                        total_sasa += sasa
                        residue_count += 1
                        sasa_per_residue.append(
                            {
                                "residue": residue.get_resname(),
                                "position": residue.id[1],
                                "sasa": round(sasa, 2),
                            }
                        )
                break

    return {
        "chain_id": chain_id,
        "total_sasa": round(total_sasa, 2),
        "residue_count": residue_count,
        "average_sasa": round(total_sasa / residue_count, 2)
        if residue_count > 0
        else 0,
        "sasa_per_residue": sasa_per_residue[:20],  # First 20 residues
    }


def calculate_rsa(pdb_file: str, chain_id: str = "A") -> Dict[str, Any]:
    """Calculate Relative Solvent Accessibility (RSA)."""
    if not PDB_AVAILABLE:
        return {"error": "Bio.PDB not available"}

    # Reference values for max ASA (from Miller et al., 1987)
    max_asa = {
        "ALA": 129.0,
        "ARG": 274.0,
        "ASN": 195.0,
        "ASP": 193.0,
        "CYS": 167.0,
        "GLN": 214.0,
        "GLU": 223.0,
        "GLY": 104.0,
        "HIS": 224.0,
        "ILE": 197.0,
        "LEU": 201.0,
        "LYS": 236.0,
        "MET": 204.0,
        "PHE": 240.0,
        "PRO": 159.0,
        "SER": 155.0,
        "THR": 172.0,
        "TRP": 285.0,
        "TYR": 263.0,
        "VAL": 174.0,
    }

    parser = PDBParser(QUIET=True)
    structure = parser.get_structure("protein", pdb_file)

    # Calculate SASA first
    sr = SASA.ShrakeRupley()
    sr.compute(structure, level="R")

    exposed_count = 0
    buried_count = 0
    rsa_values = []

    RSA_THRESHOLD = 0.25  # 25% relative accessibility threshold

    for model in structure:
        for chain in model:
            if chain.id == chain_id:
                for residue in chain:
                    if "SASA" in residue.xtra:
                        res_name = residue.get_resname()
                        if res_name in max_asa:
                            sasa = residue.xtra["SASA"]
                            rsa = sasa / max_asa[res_name]

                            if rsa > RSA_THRESHOLD:
                                exposed_count += 1
                            else:
                                buried_count += 1

                            rsa_values.append(
                                {
                                    "residue": res_name,
                                    "position": residue.id[1],
                                    "rsa": round(rsa, 3),
                                }
                            )
                break

    total = exposed_count + buried_count

    return {
        "chain_id": chain_id,
        "exposed_residues": exposed_count,
        "buried_residues": buried_count,
        "total_residues": total,
        "exposed_percentage": round(exposed_count / total * 100, 1) if total > 0 else 0,
        "rsa_values": rsa_values[:20],  # First 20 residues
    }


def format_property_report(results: Dict[str, Any]) -> str:
    """Format property calculation results as markdown report."""
    output = "# ⚗️ Physical & Chemical Properties Analysis\n\n"

    properties = results.get("properties", {})
    file_type = results.get("file_type", "fasta")
    chain_id = results.get("chain_id", "N/A")

    # Basic properties
    output += "## Basic Physicochemical Properties\n\n"
    output += f"**Sequence ID:** {properties.get('sequence_id', 'N/A')}\n\n"

    output += "| Property | Value |\n"
    output += "|:---------|:------|\n"
    output += (
        f"| **Sequence Length** | {properties.get('sequence_length', 'N/A')} aa |\n"
    )
    output += f"| **Molecular Weight** | {properties.get('molecular_weight', 0) / 1000:.3f} kDa |\n"
    output += f"| **Theoretical pI** | {properties.get('theoretical_pI', 'N/A')} |\n"
    output += f"| **Aromaticity** | {properties.get('aromaticity', 'N/A')} |\n"
    output += f"| **Instability Index** | {properties.get('instability_index', 'N/A')} "

    if properties.get("instability_index", 0) > 40:
        output += "⚠️ (Predicted Unstable) |\n"
    else:
        output += "✅ (Predicted Stable) |\n"

    output += f"| **GRAVY (Hydropathy)** | {properties.get('gravy', 'N/A')} |\n"

    # Secondary structure
    ssf = properties.get("secondary_structure_fraction", {})
    if ssf:
        output += "\n## Secondary Structure Prediction\n\n"
        output += "| Structure | Fraction |\n"
        output += "|:----------|:---------|\n"
        output += f"| **Alpha Helix** | {ssf.get('helix', 0):.1%} |\n"
        output += f"| **Beta Turn** | {ssf.get('turn', 0):.1%} |\n"
        output += f"| **Beta Sheet** | {ssf.get('sheet', 0):.1%} |\n"

    # SASA (if PDB input)
    if "sasa" in results and "error" not in results["sasa"]:
        sasa = results["sasa"]
        output += "\n## Solvent Accessible Surface Area (SASA)\n\n"
        output += f"**Chain {sasa.get('chain_id', chain_id)} Analysis:**\n\n"
        output += f"- **Total SASA:** {sasa.get('total_sasa', 0):.2f} Å²\n"
        output += f"- **Residues Analyzed:** {sasa.get('residue_count', 0)}\n"
        output += (
            f"- **Average SASA per residue:** {sasa.get('average_sasa', 0):.2f} Å²\n"
        )

    # RSA (if PDB input)
    if "rsa" in results and "error" not in results["rsa"]:
        rsa = results["rsa"]
        output += "\n## Relative Solvent Accessibility (RSA)\n\n"
        output += f"**Chain {rsa.get('chain_id', chain_id)} Analysis:**\n\n"
        output += f"- **Exposed Residues:** {rsa.get('exposed_residues', 0)} ({rsa.get('exposed_percentage', 0):.1f}%)\n"
        output += f"- **Buried Residues:** {rsa.get('buried_residues', 0)}\n"
        output += f"- **Total Residues:** {rsa.get('total_residues', 0)}\n"

    # Summary
    output += "\n---\n\n"
    output += "## Analysis Summary\n\n"
    output += f"- **Input File Type:** {file_type.upper()}\n"
    output += f"- **Chain Analyzed:** {chain_id}\n"
    output += (
        f"- **Analysis Timestamp:** {results.get('analysis_timestamp', 'N/A')}\n\n"
    )
    output += "*Property calculations completed using VenusFactory's integrated analysis pipeline.*\n"

    return output


def run_property_calculation(
    fasta_file: str = None,
    pdb_file: str = None,
    chain_id: str = "A",
    output_file: str = None,
) -> Tuple[Dict[str, Any], str]:
    """
    Main function to run property calculation.
    """
    results = {
        "analysis_timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "chain_id": chain_id,
    }

    sequence = None
    seq_id = "input"

    # Determine input type and get sequence
    if pdb_file:
        print(f"Analyzing PDB file: {pdb_file}")
        results["file_type"] = "pdb"
        results["input_file"] = pdb_file

        if PDB_AVAILABLE:
            sequence = extract_seq_from_pdb(pdb_file, chain_id)
            seq_id = os.path.basename(pdb_file).replace(".pdb", "")

            # Calculate SASA
            print("  Calculating SASA...")
            results["sasa"] = calculate_sasa(pdb_file, chain_id)

            # Calculate RSA
            print("  Calculating RSA...")
            results["rsa"] = calculate_rsa(pdb_file, chain_id)
        else:
            print("Warning: PDB analysis not available. Using sequence-only mode.")
            results["file_type"] = "fasta"

    if fasta_file or (pdb_file and not PDB_AVAILABLE):
        if not fasta_file and pdb_file:
            # Extract sequence from PDB and save as temp FASTA
            sequence = extract_seq_from_pdb(pdb_file, chain_id)
            seq_id = os.path.basename(pdb_file).replace(".pdb", "")
        else:
            print(f"Reading FASTA file: {fasta_file}")
            results["file_type"] = "fasta"
            results["input_file"] = fasta_file
            record = next(SeqIO.parse(fasta_file, "fasta"))
            sequence = str(record.seq).replace("*", "").replace("X", "C")
            seq_id = record.id

    if not sequence:
        raise ValueError("No sequence found. Please provide a valid FASTA or PDB file.")

    print(f"Sequence: {seq_id}, Length: {len(sequence)} aa")

    # Calculate physicochemical properties
    print("Calculating physicochemical properties...")
    results["properties"] = calculate_physicochemical(sequence)
    results["properties"]["sequence_id"] = seq_id

    # Save results
    if output_file:
        with open(output_file, "w", encoding="utf-8") as f:
            json.dump(results, f, indent=2, ensure_ascii=False)
        print(f"Results saved to: {output_file}")

    # Format report
    report = format_property_report(results)

    return results, report


def main():
    parser = argparse.ArgumentParser(
        description="⚗️ Protein Physical & Chemical Properties Calculator - Based on VenusFactory",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
    python property_calculator.py --fasta_file protein.fasta
    python property_calculator.py --pdb_file structure.pdb
    python property_calculator.py --pdb_file structure.pdb --chain_id B
    python property_calculator.py --fasta_file input.fasta --output results.json
        """,
    )

    parser.add_argument("--fasta_file", type=str, help="Input FASTA file path")
    parser.add_argument("--pdb_file", type=str, help="Input PDB file path")
    parser.add_argument(
        "--chain_id", type=str, default="A", help="Chain ID for PDB files (default: A)"
    )
    parser.add_argument("--output", type=str, help="Output JSON file path")

    args = parser.parse_args()

    if not args.fasta_file and not args.pdb_file:
        parser.error("Please provide either --fasta_file or --pdb_file")

    print("=" * 60)
    print("⚗️ VenusFactory - Physical & Chemical Properties Calculator")
    print("=" * 60)
    print(f"Timestamp: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print()

    try:
        results, report = run_property_calculation(
            fasta_file=args.fasta_file,
            pdb_file=args.pdb_file,
            chain_id=args.chain_id,
            output_file=args.output,
        )

        print()
        print("=" * 60)
        print("📊 PROPERTY CALCULATION RESULTS")
        print("=" * 60)
        print(report)

    except Exception as e:
        print(f"❌ Error: {str(e)}")
        import traceback

        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
