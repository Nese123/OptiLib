# OptiLib

OptiLib is a drug library optimization platform combining a modern Flask web application with a multi-objective genetic algorithm (NSGA-II). It enables researchers to design cost-effective, highly selective small-molecule drug libraries tailored to custom sets of biological targets.

## Overview

OptiLib provides two flexible pipelines to generate and optimize candidate compound libraries:

1. **Target-Driven Pipeline (ChEMBL):**
   * **Target Input & Validation:** Upload target identifiers (gene symbols, UniProt accessions, ChEMBL IDs, or protein names) with instant validation against a local ChEMBL 36 database.
   * **Bioactivity Querying & Matrix Building:** Extracts affinity data, computes compound selectivities across targets, and applies a customizable selectivity threshold.

2. **Custom Affinity Pipeline:**
   * **Experimental Data Upload:** Upload multi-file custom affinity datasets (compound, target, affinity value) with automatic identifier resolution (InChIKeys, ChEMBL IDs, SMILES) and real-time dataset management.

3. **Multi-Tier Price Estimation:**
   * **Custom Price Upload:** Incorporates user-supplied compound pricing ($/mg) across multiple files.
   * **MolPort Commercial Database:** Automatic price lookup against stock compound databases.
   * **Machine Learning Fallback (MolPrice):** Predicts market prices using machine learning models trained on molecular fingerprints for compounds without known commercial catalog prices.

4. **Multi-Objective Genetic Optimization (NSGA-II):**
   * Solves the dual objectives of **minimizing total library cost** while **maximizing compound selectivity and target coverage**.
   * Supports custom balancing weights (mean vs. minimum target selectivity), maximum allowed budget limits, missed target constraints, and customizable genetic algorithm parameters (population size, max generations, tolerance).
   * Features real-time generation tracking and early-stopping controls.

5. **Interactive Results & Exploration Dashboard:**
   * **Interactive Pareto Front:** Visualizes the cost-selectivity trade-off curve with automatic "Best Trade-off" knee-point detection and click-to-select alternative solutions.
   * **Selectivity Heatmap:** Instant interactive visualization of compound-target affinities across the winning library.
   * **Export:** One-click download of the optimized library and selectivity matrices in Excel (`.xlsx`) format.

## Project Structure

* `webapp/` - Main Flask application directory.
  * `app.py` - Flask web server, API routes, rate limiting, and session state management.
  * `core/` - Core computational modules:
    * `algorithm.py` - NSGA-II optimization problem definition (`pymoo`), smart initialization, and Pareto analysis.
    * `selectivity.py` - Selectivity matrix construction and ChEMBL SQL extraction.
  * `templates/` & `static/` - Modern UI interface, styles, charts, and interactive dashboards.
  * `output/` - Session-scoped directory for generated matrices and export files.
* `MolPrice/` - Machine learning module for compound price prediction from chemical structures.
* `database/` - Local storage for compound and bioactivity databases (`chembl_36.db`, `molport.db`).
* `scripts/` - Maintenance utilities, including `update_molport_db.py` for monthly MolPort FTP syncs.
* `nginx/` - Production reverse proxy configuration with TLS/HTTPS support.
* `DEPLOYMENT.md` - Production server deployment guide.

## Technologies Used

* **Web Framework:** Flask, Werkzeug, Gunicorn
* **Optimization Engine:** `pymoo` (NSGA-II algorithm)
* **Cheminformatics:** RDKit
* **Data Processing & ML:** `numpy`, `pandas`, `scikit-learn`
* **Security & Scalability:** `flask-limiter`, SQLite (WAL mode), Docker & Docker Compose

