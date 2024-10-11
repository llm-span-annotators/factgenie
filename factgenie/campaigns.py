#!/usr/bin/env python3
import os
import json
import glob
import logging
import pandas as pd
import ast
import coloredlogs

from pathlib import Path

logger = logging.getLogger(__name__)
# coloredlogs.install(level="INFO", logger=logger, fmt="%(asctime)s %(levelname)s %(message)s")


DIR_PATH = os.path.dirname(__file__)
ANNOTATIONS_DIR = os.path.join(DIR_PATH, "annotations")
GENERATIONS_DIR = os.path.join(DIR_PATH, "generations")


class CampaignStatus:
    IDLE = "idle"
    RUNNING = "running"
    FINISHED = "finished"


class ExampleStatus:
    FREE = "free"
    ASSIGNED = "assigned"
    FINISHED = "finished"


class Campaign:
    @classmethod
    def get_name(cls):
        return cls.__name__

    @classmethod
    def get_main_dir(cls):
        return ANNOTATIONS_DIR

    def __init__(self, campaign_id):
        self.campaign_id = campaign_id
        self.dir = os.path.join(self.__class__.get_main_dir(), campaign_id)
        self.db_path = os.path.join(self.dir, "db.csv")
        self.metadata_path = os.path.join(self.dir, "metadata.json")

        self.load_db()
        self.load_metadata()

        # temporary fix for the old campaigns
        if self.metadata.get("status") in ["new", "paused"]:
            self.metadata["status"] = CampaignStatus.IDLE
            self.update_metadata()

        # if the db does not contain the `end` column, add it
        if "end" not in self.db.columns:
            self.db["end"] = ""
            self.update_db(self.db)

    def get_finished_examples(self):
        # load all the JSONL files in the "files" subdirectory
        examples_finished = []

        for jsonl_file in glob.glob(os.path.join(self.dir, "files/*.jsonl")):
            with open(jsonl_file) as f:
                for line in f:
                    example = json.loads(line)
                    examples_finished.append(example)

        return examples_finished

    def update_db(self, db):
        self.db = db
        db.to_csv(self.db_path, index=False)

    def load_db(self):
        with open(self.db_path) as f:
            self.db = pd.read_csv(f)

    def update_metadata(self):
        with open(self.metadata_path, "w") as f:
            json.dump(self.metadata, f, indent=4)

    def load_metadata(self):
        with open(self.metadata_path) as f:
            self.metadata = json.load(f)

    def clear_all_outputs(self):
        # remove files
        for jsonl_file in glob.glob(os.path.join(self.dir, "files/*.jsonl")):
            os.remove(jsonl_file)

        self.db["status"] = ExampleStatus.FREE
        self.db["annotator_id"] = ""
        self.db["start"] = ""
        self.update_db(self.db)

        self.metadata["status"] = CampaignStatus.IDLE
        self.update_metadata()

    def clear_single_output(self, idx, idx_type="example_idx"):
        # Identify the rows where idx_type matches idx
        mask = self.db[idx_type] == idx

        # Update the DataFrame using .loc
        self.db.loc[mask, "status"] = ExampleStatus.FREE
        self.db.loc[mask, "annotator_id"] = ""
        self.db.loc[mask, "start"] = ""

        self.update_db(self.db)

        if self.metadata["status"] == CampaignStatus.FINISHED:
            self.metadata["status"] = CampaignStatus.IDLE
            self.update_metadata()

        # TODO should we remove the output file as well?


class ExternalCampaign(Campaign):
    def get_stats(self):
        return {}


class HumanCampaign(Campaign):
    def get_examples_for_batch(self, batch_idx):
        annotator_batch = []

        # find all examples for this batch in self.db
        batch_examples = self.db[self.db["batch_idx"] == batch_idx]

        for _, row in batch_examples.iterrows():
            annotator_batch.append(
                {
                    "dataset": row["dataset"],
                    "split": row["split"],
                    "setup_id": row["setup_id"],
                    "example_idx": row["example_idx"],
                    "annotator_group": row["annotator_group"],
                }
            )
        return annotator_batch

    def get_overview(self):
        self.load_db()
        overview_db = self.db.copy()
        # replace NaN with empty string
        overview_db = overview_db.where(pd.notnull(overview_db), "")

        # group by batch idx
        # add a column with the number of examples for each batch
        # for other columns keep first item
        overview_db = overview_db.groupby("batch_idx").agg(
            {
                "dataset": "first",
                "split": "first",
                "example_idx": "count",
                "setup_id": "first",
                "status": "first",
                "start": "first",
                "end": "first",
                "annotator_id": "first",
                "annotator_group": "first",
            }
        )

        overview_db["example_details"] = overview_db.index.map(lambda batch_idx: self.get_examples_for_batch(batch_idx))

        overview_db = overview_db.rename(columns={"example_idx": "example_cnt"}).reset_index()
        overview_db = overview_db.to_dict(orient="records")

        return overview_db

    def get_stats(self):
        # group by batch_idx, keep the first row of each group
        batch_stats = self.db.groupby("batch_idx").first()

        return {
            "total": len(batch_stats),
            "assigned": len(batch_stats[batch_stats["status"] == ExampleStatus.ASSIGNED]),
            "finished": len(batch_stats[batch_stats["status"] == ExampleStatus.FINISHED]),
            "free": len(batch_stats[batch_stats["status"] == ExampleStatus.FREE]),
        }

    def clear_output(self, idx):
        self.clear_single_output(idx, idx_type="batch_idx")


class LLMCampaign(Campaign):
    def get_stats(self):
        return {
            "total": len(self.db),
            "finished": len(self.db[self.db["status"] == ExampleStatus.FINISHED]),
            "free": len(self.db[self.db["status"] == ExampleStatus.FREE]),
        }

    def clear_output(self, idx):
        self.clear_single_output(idx, idx_type="example_idx")


class LLMCampaignEval(LLMCampaign):
    def get_overview(self):
        # pair the examples in db with the finished examples
        # we need to match the examples on (dataset, split, setup, example_idx)
        # add the annotations to the df

        # get the finished examples
        finished_examples = self.get_finished_examples()
        example_index = {
            (ex["dataset"], ex["split"], ex["setup_id"], ex["example_idx"]): str(ex) for ex in finished_examples
        }

        self.load_db()
        overview_db = self.db.copy()
        overview_db["output"] = ""

        for i, row in self.db.iterrows():
            key = (row["dataset"], row["split"], row["setup_id"], row["example_idx"])
            example = ast.literal_eval(example_index.get(key, "{}"))

            annotations = example.get("annotations", [])
            overview_db.at[i, "output"] = str(annotations)

        overview_db = overview_db.to_dict(orient="records")

        return overview_db


class LLMCampaignGen(LLMCampaign):
    @classmethod
    def get_main_dir(cls):
        return GENERATIONS_DIR

    def get_overview(self):
        finished_examples = self.get_finished_examples()

        example_index = {(ex["dataset"], ex["split"], ex["example_idx"]): str(ex) for ex in finished_examples}

        self.load_db()
        overview_db = self.db.copy()
        overview_db["output"] = ""

        for i, row in self.db.iterrows():
            key = (row["dataset"], row["split"], row["example_idx"])
            example = ast.literal_eval(example_index.get(key, "{}"))

            overview_db.at[i, "output"] = str(example.get("out", ""))

        overview_db = overview_db.to_dict(orient="records")
        return overview_db
