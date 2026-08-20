# OptiLib

OptiLib is a drug library optimization platform built with a Flask web application and a powerful multi-objective genetic algorithm, NSGA-II. It is designed to help researchers select the optimal set of chemical compounds to a given set of targets that maximizes biological target coverage and selectivity while minimizing financial cost.

## Overview

The OptiLib pipeline performs the following steps:
1. **Target Input:** Users upload or define a set of biological targets of interest.
2. **Database Querying:** The system queries local databases (e.g., ChEMBL) to find compounds active against the specified targets.
3. **Selectivity Matrix Generation:** It builds a selectivity matrix, scoring each compound against the targets.
4. **Price Estimation:** It utilizes the MolPort database and the integrated `MolPrice` module to estimate or retrieve pricing for the candidate compounds.
5. **Optimization:** It runs the NSGA-II (Non-dominated Sorting Genetic Algorithm II) algorithm via the `pymoo` library to find the Pareto optimal libraries—balancing high biological score (selectivity and target coverage) against low total cost.
6. **Results Dashboard:** Finally, the web interface presents the optimal solutions (the Pareto front), allowing researchers to explore trade-offs and select the winning library.

## Project Structure

* `webapp/` - The main Flask application directory.
  * `app.py` - The entry point for the Flask web server.
  * `core/` - Contains the core logic, including the NSGA-II problem definition and selectivity matrix generation (`algorithm.py`, `selectivity.py`).
  * `templates/` & `static/` - HTML, CSS, and JS files for the web interface.
  * `output/` - Directory where results, plots, and optimal library matrices are saved.
* `MolPrice/` - A submodule used for compound price predictions.
* `database/` - Local storage for compound databases (e.g., `chembl_36.db`, `molport.db`).

## Setup and Installation

1. Create and activate a Python virtual environment.
2. Install the required dependencies:
   ```bash
   pip install -r requirements.txt
   ```
3. Ensure the `database/` directory contains the necessary database files (`chembl_36.db`, `molport.db`).
4. Ensure the `MolPrice` submodule is initialized and its dependencies are satisfied.

## Running the Application

To start the OptiLib web application, run:

```bash
python webapp/app.py
```

The application will typically be accessible at `http://127.0.0.1:5000/`.

## Running with Docker

You can run OptiLib in a containerized environment using Docker or Docker Compose.

> **Note:** The SQLite databases in `database/` (`chembl_36.db` and `molport.db`, totaling ~32 GB) are mounted as volumes at runtime rather than baked into the Docker image.

### Option 1: Docker Compose (Recommended)

```bash
# Build and run in the background
docker compose up --build -d

# View logs
docker compose logs -f

# Stop the container
docker compose down
```

### Option 2: Docker CLI

```bash
# Build the Docker image
docker build -t optilib .

# Run the container with database and output volume mounts
docker run -d \
  -p 5000:5000 \
  -v $(pwd)/database:/app/database \
  -v $(pwd)/webapp/output:/app/webapp/output \
  --name optilib \
  optilib
```

Access the web interface at `http://localhost:5000/`.

## Technologies Used

* **Web Framework:** Flask
* **Optimization:** `pymoo` (NSGA-II)
* **Data Processing:** `numpy`, `pandas`
* **File Handling:** `openpyxl`, `xlsxwriter`

