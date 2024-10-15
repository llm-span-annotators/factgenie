#!/usr/bin/env python3
import os
import json
import time
import logging
import pandas as pd
import time
import threading
import traceback
import shutil
import datetime
import zipfile
from flask import (
    Flask,
    render_template,
    jsonify,
    request,
    Response,
    make_response,
    redirect,
    url_for,
    send_from_directory,
)
from collections import defaultdict
import urllib.parse
from slugify import slugify

from factgenie.campaigns import HumanCampaign, CampaignStatus, ExampleStatus, ANNOTATIONS_DIR, GENERATIONS_DIR
from factgenie.models import ModelFactory
from factgenie.loaders.dataset import get_dataset_classes
import factgenie.utils as utils
import factgenie.analysis as analysis

from werkzeug.middleware.proxy_fix import ProxyFix

DIR_PATH = os.path.dirname(__file__)
TEMPLATES_DIR = os.path.join(DIR_PATH, "templates")
STATIC_DIR = os.path.join(DIR_PATH, "static")


app = Flask("factgenie", template_folder=TEMPLATES_DIR, static_folder=STATIC_DIR)
app.db = {}
app.db["annotation_index"] = {}
app.db["lock"] = threading.Lock()
app.db["threads"] = {}
app.db["announcers"] = {}
app.wsgi_app = ProxyFix(app.wsgi_app, x_host=1)

logger = logging.getLogger(__name__)


# -----------------
# Jinja filters
# -----------------
@app.template_filter("ctime")
def timectime(timestamp):
    try:
        s = datetime.datetime.fromtimestamp(timestamp)
        return s.strftime("%Y-%m-%d %H:%M:%S")
    except:
        return timestamp


@app.template_filter("elapsed")
def time_elapsed(batch):
    start_timestamp = batch["start"]
    end_timestamp = batch["end"]
    try:
        if end_timestamp:
            s = datetime.datetime.fromtimestamp(start_timestamp)
            e = datetime.datetime.fromtimestamp(end_timestamp)
            diff = str(e - s)
            return diff.split(".")[0]
        else:

            s = datetime.datetime.fromtimestamp(start_timestamp)
            diff = str(datetime.datetime.now() - s)
            return diff.split(".")[0]
    except:
        return ""


@app.template_filter("annotate_url")
def annotate_url(current_url):
    # get the base url (without any "browse", "crowdsourcing" or "crowdsourcing/campaign" in it)
    parsed = urllib.parse.urlparse(current_url)
    base_url = f"{parsed.scheme}://{parsed.netloc}"
    host_prefix = app.config["host_prefix"]
    return f"{base_url}{host_prefix}/annotate"


@app.template_filter("prettify_json")
def prettify_json(value):
    return json.dumps(value, sort_keys=True, indent=4, separators=(",", ": "))


# -----------------
# Decorators
# -----------------


# Very simple decorator to protect routes
def login_required(f):
    def wrapper(*args, **kwargs):
        if app.config["login"]["active"]:
            auth = request.cookies.get("auth")
            if not auth:
                return redirect(app.config["host_prefix"] + "/login")
            username, password = auth.split(":")
            if not utils.check_login(app, username, password):
                return redirect(app.config["host_prefix"] + "/login")

        return f(*args, **kwargs)

    wrapper.__name__ = f.__name__
    return wrapper


# -----------------
# Flask endpoints
# -----------------
@app.route("/", methods=["GET", "POST"])
@login_required
def index():
    logger.info(f"Main page loaded")

    return render_template(
        "index.html",
        host_prefix=app.config["host_prefix"],
    )


@app.route("/about", methods=["GET", "POST"])
@login_required
def about():
    logger.info(f"About page loaded")

    return render_template(
        "about.html",
        host_prefix=app.config["host_prefix"],
    )


@app.route("/analyze", methods=["GET", "POST"])
@login_required
def analyze():
    logger.info(f"Analysis page loaded")

    campaigns = utils.get_sorted_campaign_list(app, sources=["crowdsourcing", "llm_eval"])

    return render_template(
        "analyze.html",
        campaigns=campaigns,
        host_prefix=app.config["host_prefix"],
    )


@app.route("/analyze/detail", methods=["GET", "POST"])
@login_required
def analyze_detail():
    campaign_id = request.args.get("campaign")
    source = request.args.get("source")

    campaign = utils.load_campaign(app, campaign_id=campaign_id, mode=source)

    datasets = utils.get_local_dataset_overview(app)
    statistics = analysis.compute_statistics(app, campaign, datasets)

    return render_template(
        "analyze_detail.html",
        statistics=statistics,
        campaign=campaign,
        source=source,
        host_prefix=app.config["host_prefix"],
    )


@app.route("/annotate", methods=["GET", "POST"])
def annotate():
    logger.info(f"Annotate page loaded")

    campaign_id = request.args.get("campaign")
    campaign = utils.load_campaign(app, campaign_id=campaign_id, mode="crowdsourcing")

    service = campaign.metadata["config"]["service"]
    service_ids = utils.get_service_ids(service, request.args)

    db = campaign.db
    metadata = campaign.metadata
    annotation_set = utils.get_annotator_batch(app, campaign, db, service_ids)

    if not annotation_set:
        # no more available examples
        return render_template(
            "campaigns/closed.html",
            host_prefix=app.config["host_prefix"],
            metadata=metadata,
        )

    return render_template(
        f"campaigns/{campaign.campaign_id}/annotate.html",
        host_prefix=app.config["host_prefix"],
        annotation_set=annotation_set,
        annotator_id=service_ids["annotator_id"],
        metadata=metadata,
    )


@app.route("/browse", methods=["GET", "POST"])
@login_required
def browse():
    logger.info(f"Browse page loaded")

    utils.generate_annotation_index(app)

    dataset_id = request.args.get("dataset")
    split = request.args.get("split")
    example_idx = request.args.get("example_idx")

    if dataset_id and split and example_idx:
        display_example = {"dataset": dataset_id, "split": split, "example_idx": int(example_idx)}
        logger.info(f"Serving permalink for {display_example}")
    else:
        display_example = None

    datasets = utils.get_local_dataset_overview(app)
    datasets = {k: v for k, v in datasets.items() if v["enabled"]}

    if not datasets:
        return render_template(
            "no_datasets.html",
            host_prefix=app.config["host_prefix"],
        )

    return render_template(
        "browse.html",
        display_example=display_example,
        datasets=datasets,
        host_prefix=app.config["host_prefix"],
        annotations=app.db["annotation_index"],
    )


@app.route("/clear_campaign", methods=["GET", "POST"])
@login_required
def clear_campaign():
    data = request.get_json()
    campaign_id = data.get("campaignId")
    mode = data.get("mode")

    campaign = utils.load_campaign(app, campaign_id=campaign_id, mode=mode)
    campaign.clear_all_outputs()

    return utils.success()


@app.route("/clear_output", methods=["GET", "POST"])
@login_required
def clear_output():
    data = request.get_json()
    campaign_id = data.get("campaignId")
    mode = data.get("mode")
    idx = int(data.get("idx"))

    campaign = utils.load_campaign(app, campaign_id=campaign_id, mode=mode)
    campaign.clear_output(idx)

    return utils.success()


@app.route("/crowdsourcing", methods=["GET", "POST"])
@login_required
def crowdsourcing():
    logger.info(f"Crowdsourcing page loaded")

    campaign_index = utils.generate_campaign_index(app, force_reload=True)

    llm_configs = utils.load_configs(mode="llm_eval")
    crowdsourcing_configs = utils.load_configs(mode="crowdsourcing")

    campaigns = defaultdict(dict)

    for campaign_id, campaign in sorted(
        campaign_index["crowdsourcing"].items(), key=lambda x: x[1].metadata["created"], reverse=True
    ):
        campaigns[campaign_id]["metadata"] = campaign.metadata
        campaigns[campaign_id]["stats"] = campaign.get_stats()

    return render_template(
        "crowdsourcing.html",
        campaigns=campaigns,
        llm_configs=llm_configs,
        crowdsourcing_configs=crowdsourcing_configs,
        is_password_protected=app.config["login"]["active"],
        host_prefix=app.config["host_prefix"],
    )


@app.route("/crowdsourcing/detail", methods=["GET", "POST"])
@login_required
def crowdsourcing_detail():

    campaign_id = request.args.get("campaign")
    campaign = utils.load_campaign(app, campaign_id=campaign_id, mode="crowdsourcing")

    overview = campaign.get_overview()
    stats = campaign.get_stats()

    return render_template(
        "crowdsourcing_detail.html",
        mode="crowdsourcing",
        campaign_id=campaign_id,
        overview=overview,
        stats=stats,
        metadata=campaign.metadata,
        host_prefix=app.config["host_prefix"],
    )


@app.route("/crowdsourcing/create", methods=["POST"])
@login_required
def crowdsourcing_create():
    data = request.get_json()

    campaign_id = slugify(data.get("campaignId"))
    campaign_data = data.get("campaignData")
    config = data.get("config")

    config = utils.parse_crowdsourcing_config(config)

    # create a new directory
    if os.path.exists(os.path.join(ANNOTATIONS_DIR, campaign_id)):
        return jsonify({"error": "Campaign already exists"})

    os.makedirs(os.path.join(ANNOTATIONS_DIR, campaign_id, "files"), exist_ok=True)

    # create the annotation CSV
    db = utils.generate_campaign_db(app, campaign_data, config=config)
    db.to_csv(os.path.join(ANNOTATIONS_DIR, campaign_id, "db.csv"), index=False)

    # save metadata
    with open(os.path.join(ANNOTATIONS_DIR, campaign_id, "metadata.json"), "w") as f:
        json.dump(
            {
                "id": campaign_id,
                "source": "crowdsourcing",
                "config": config,
                "created": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            },
            f,
            indent=4,
        )

    # prepare the crowdsourcing HTML page
    utils.create_crowdsourcing_page(campaign_id, config)
    utils.load_campaign(app, campaign_id=campaign_id, mode="crowdsourcing")

    return utils.success()


@app.route("/crowdsourcing/new", methods=["GET", "POST"])
@login_required
def crowdsourcing_new():
    datasets = utils.get_local_dataset_overview(app)
    datasets = {k: v for k, v in datasets.items() if v["enabled"]}

    model_outs = utils.get_model_outputs_overview(app, datasets, non_empty=True)

    configs = utils.load_configs(mode="crowdsourcing")

    campaign_index = utils.generate_campaign_index(app, force_reload=False)
    default_campaign_id = utils.generate_default_id(campaign_index=campaign_index["crowdsourcing"], prefix="campaign")

    return render_template(
        "crowdsourcing_new.html",
        default_campaign_id=default_campaign_id,
        datasets=datasets,
        model_outs=model_outs,
        configs=configs,
        host_prefix=app.config["host_prefix"],
    )


@app.route("/compute_agreement", methods=["POST"])
@login_required
def compute_agreement():
    data = request.get_json()
    combinations = data.get("combinations")
    selected_campaigns = data.get("selectedCampaigns")

    campaign_index = utils.generate_campaign_index(app, force_reload=True)
    # flatten the campaigns
    campaigns = {k: v for source in campaign_index.values() for k, v in source.items()}

    datasets = utils.get_local_dataset_overview(app)

    try:
        results = analysis.compute_inter_annotator_agreement(
            app,
            selected_campaigns=selected_campaigns,
            combinations=combinations,
            campaigns=campaigns,
            datasets=datasets,
        )
        return jsonify(results)
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": f"Error while computing agreement: {e}"})


@app.route("/delete_campaign", methods=["POST"])
@login_required
def delete_campaign():
    data = request.get_json()
    campaign_name = data.get("campaignId")
    mode = data.get("mode")

    if mode == "llm_gen":
        target_dir = GENERATIONS_DIR
    else:
        target_dir = ANNOTATIONS_DIR

    shutil.rmtree(os.path.join(target_dir, campaign_name))

    if os.path.exists(os.path.join(TEMPLATES_DIR, "campaigns", campaign_name)):
        shutil.rmtree(os.path.join(TEMPLATES_DIR, "campaigns", campaign_name))

    return utils.success()


@app.route("/delete_dataset", methods=["POST"])
@login_required
def delete_dataset():
    data = request.get_json()
    dataset_id = data.get("datasetId")

    utils.delete_dataset(app, dataset_id)

    return utils.success()


@app.route("/delete_model_outputs", methods=["POST"])
@login_required
def delete_model_outputs():
    data = request.get_json()

    # get dataset, split, setup
    dataset_id = data.get("dataset")
    split = data.get("split")
    setup_id = data.get("setup_id")

    dataset = app.db["datasets_obj"][dataset_id]
    utils.delete_model_outputs(dataset, split, setup_id)

    return utils.success()


@app.route("/download_dataset", methods=["POST"])
@login_required
def download_dataset():
    data = request.get_json()
    dataset_id = data.get("datasetId")

    try:
        utils.download_dataset(app, dataset_id)
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": f"Error while downloading dataset: {e}"})

    return utils.success()


@app.route("/duplicate_config", methods=["POST"])
def duplicate_config():
    data = request.get_json()
    filename = data.get("filename")
    mode_from = data.get("modeFrom")
    mode_to = data.get("modeTo")
    campaign_id = data.get("campaignId")

    campaign_index = utils.generate_campaign_index(app, force_reload=False)

    if mode_from == mode_to:
        campaign = campaign_index[mode_from][campaign_id]
        config = campaign.metadata["config"]
    else:
        # currently we only support copying the annotation_span_categories between modes
        campaign = campaign_index[mode_from][campaign_id]
        llm_config = campaign.metadata["config"]
        config = {"annotation_span_categories": llm_config["annotation_span_categories"]}

    utils.save_config(filename, config, mode=mode_to)

    return utils.success()


@app.route("/duplicate_eval", methods=["POST"])
def duplicate_eval():
    data = request.get_json()
    mode = data.get("mode")
    campaign_id = data.get("campaignId")
    new_campaign_id = slugify(data.get("newCampaignId"))

    ret = utils.duplicate_eval(app, mode, campaign_id, new_campaign_id)

    return ret


@app.route("/example", methods=["GET", "POST"])
def render_example():
    dataset_id = request.args.get("dataset")
    split = request.args.get("split")
    example_idx = int(request.args.get("example_idx"))

    try:
        example_data = utils.get_example_data(app, dataset_id, split, example_idx)
        return jsonify(example_data)
    except Exception as e:
        traceback.print_exc()
        logger.error(f"Error while getting example data: {e}")
        logger.error(f"{dataset_id=}, {split=}, {example_idx=}")
        return jsonify({"error": f"Error\n\t{e}\nwhile getting example data: {dataset_id=}, {split=}, {example_idx=}"})


@app.route("/export_campaign_outputs", methods=["GET", "POST"])
@login_required
def export_campaign_outputs():
    campaign_id = request.args.get("campaign")
    mode = request.args.get("mode")

    return utils.export_campaign_outputs(app, mode, campaign_id)


@app.route("/export_dataset", methods=["GET", "POST"])
@login_required
def export_dataset():
    dataset_id = request.args.get("dataset_id")

    return utils.export_dataset(app, dataset_id)


@app.route("/export_outputs", methods=["GET", "POST"])
@login_required
def export_outputs():
    dataset_id = request.args.get("dataset")
    split = request.args.get("split")
    setup_id = request.args.get("setup_id")

    return utils.export_outputs(app, dataset_id, split, setup_id)


@app.route("/files/<path:filename>", methods=["GET"])
def download_file(filename):
    # serving external files for datasets
    return send_from_directory("data", filename)


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form["username"]
        password = request.form["password"]
        if utils.check_login(app, username, password):
            # redirect to the home page ("/")
            resp = make_response(redirect(app.config["host_prefix"] + "/"))
            resp.set_cookie("auth", f"{username}:{password}")
            return resp
        else:
            return "Login failed", 401
    return render_template("login.html", host_prefix=app.config["host_prefix"])


@app.route("/llm_campaign", methods=["GET", "POST"])
@login_required
def llm_campaign():
    logger.info(f"LLM campaign page loaded")
    mode = request.args.get("mode")

    if not mode:
        return "The `mode` argument was not specified", 404

    campaign_index = utils.generate_campaign_index(app)
    campaigns = defaultdict(dict)

    llm_configs = utils.load_configs(mode=mode)
    crowdsourcing_configs = utils.load_configs(mode="crowdsourcing")

    for campaign_id, campaign in sorted(
        campaign_index[mode].items(), key=lambda x: x[1].metadata["created"], reverse=True
    ):
        campaigns[campaign_id]["metadata"] = campaign.metadata
        campaigns[campaign_id]["stats"] = campaign.get_stats()

    return render_template(
        f"llm_campaign.html",
        mode=mode,
        llm_configs=llm_configs,
        crowdsourcing_configs=crowdsourcing_configs,
        campaigns=campaigns,
        host_prefix=app.config["host_prefix"],
    )


@app.route("/llm_campaign/create", methods=["GET", "POST"])
@login_required
def llm_campaign_create():
    mode = request.args.get("mode")

    if not mode:
        return "The `mode` argument was not specified", 404

    data = request.get_json()

    campaign_id = slugify(data.get("campaignId"))
    campaign_data = data.get("campaignData")
    config = data.get("config")

    if mode == "llm_eval":
        config = utils.parse_llm_eval_config(config)
    elif mode == "llm_gen":
        config = utils.parse_llm_gen_config(config)

    datasets = app.db["datasets_obj"]

    try:
        utils.llm_campaign_new(mode, campaign_id, config, campaign_data, datasets)
        utils.load_campaign(app, campaign_id=campaign_id, mode=mode)
    except Exception as e:
        traceback.print_exc()
        return utils.error(f"Error while creating campaign: {e}")

    return utils.success()


@app.route("/llm_campaign/detail", methods=["GET", "POST"])
@login_required
def llm_campaign_detail():
    mode = request.args.get("mode")

    if not mode:
        return "The `mode` argument was not specified", 404

    campaign_id = request.args.get("campaign")
    campaign = utils.load_campaign(app, campaign_id=campaign_id, mode=mode)

    if campaign.metadata["status"] == CampaignStatus.RUNNING and not app.db["announcers"].get(campaign_id):
        campaign.metadata["status"] = CampaignStatus.IDLE
        campaign.update_metadata()

    overview = campaign.get_overview()
    finished_examples = [x for x in overview if x["status"] == ExampleStatus.FINISHED]

    return render_template(
        f"llm_campaign_detail.html",
        mode=mode,
        campaign_id=campaign_id,
        overview=overview,
        finished_examples=finished_examples,
        metadata=campaign.metadata,
        host_prefix=app.config["host_prefix"],
    )


@app.route("/llm_campaign/new", methods=["GET", "POST"])
@login_required
def llm_campaign_new():
    mode = request.args.get("mode")

    if not mode:
        return "The `mode` argument was not specified", 404

    datasets = utils.get_local_dataset_overview(app)
    datasets = {k: v for k, v in datasets.items() if v["enabled"]}

    non_empty = True if mode == "llm_eval" else False
    model_outs = utils.get_model_outputs_overview(app, datasets, non_empty=non_empty)

    # get a list of available metrics
    llm_configs = utils.load_configs(mode=mode)
    metric_types = list(ModelFactory.model_classes()[mode].keys())

    campaign_index = utils.generate_campaign_index(app, force_reload=False)
    default_campaign_id = utils.generate_default_id(campaign_index=campaign_index[mode], prefix=mode.replace("_", "-"))

    return render_template(
        f"llm_campaign_new.html",
        mode=mode,
        datasets=datasets,
        default_campaign_id=default_campaign_id,
        model_outs=model_outs,
        configs=llm_configs,
        metric_types=metric_types,
        host_prefix=app.config["host_prefix"],
    )


@app.route("/llm_campaign/run", methods=["POST"])
@login_required
def llm_campaign_run():
    mode = request.args.get("mode")

    if not mode:
        return "The `mode` argument was not specified", 404

    data = request.get_json()
    campaign_id = data.get("campaignId")

    app.db["announcers"][campaign_id] = announcer = utils.MessageAnnouncer()

    app.db["threads"][campaign_id] = {
        "running": True,
    }

    try:
        campaign = utils.load_campaign(app, campaign_id=campaign_id, mode=mode)
        threads = app.db["threads"]
        datasets = app.db["datasets_obj"]

        config = campaign.metadata["config"]
        model = ModelFactory.from_config(config, mode=mode)

        return utils.run_llm_campaign(mode, campaign_id, announcer, campaign, datasets, model, threads)
    except Exception as e:
        traceback.print_exc()
        return utils.error(f"Error while running campaign: {e}")


@app.route("/llm_campaign/update_metadata", methods=["POST"])
@login_required
def llm_campaign_update_config():
    data = request.get_json()
    mode = request.args.get("mode")

    campaign_id = data.get("campaignId")
    config = data.get("config")

    config = utils.parse_campaign_config(config)
    campaign = utils.load_campaign(app, campaign_id=campaign_id, mode=mode)
    campaign.metadata["config"] = config
    campaign.update_metadata()

    return utils.success()


@app.route("/llm_campaign/progress/<campaign_id>", methods=["GET"])
@login_required
def listen(campaign_id):
    if not app.db["announcers"].get(campaign_id):
        return Response(status=404)

    def stream():
        messages = app.db["announcers"][campaign_id].listen()
        while True:
            msg = messages.get()
            yield msg

    return Response(stream(), mimetype="text/event-stream")


@app.route("/llm_campaign/pause", methods=["POST"])
@login_required
def llm_campaign_pause():
    mode = request.args.get("mode")

    if not mode:
        return "The `mode` argument was not specified", 404

    data = request.get_json()
    campaign_id = data.get("campaignId")
    app.db["threads"][campaign_id]["running"] = False

    campaign = utils.load_campaign(app, campaign_id=campaign_id, mode=mode)
    campaign.metadata["status"] = CampaignStatus.IDLE
    campaign.update_metadata()

    resp = jsonify(success=True, status=campaign.metadata["status"])
    return resp


@app.route("/llm_eval/detail", methods=["GET", "POST"])
@login_required
def llm_eval():
    campaign_id = request.args.get("campaign")

    # redirect to /llm_campaign with the mode set to llm_eval, keeping the campaign_id
    return redirect(f"{app.config['host_prefix']}/llm_campaign/detail?mode=llm_eval&campaign={campaign_id}")


@app.route("/llm_gen/detail", methods=["GET", "POST"])
@login_required
def llm_gen():
    campaign_id = request.args.get("campaign")

    # redirect to /llm_campaign with the mode set to llm_gen, keeping the campaign_id
    return redirect(f"{app.config['host_prefix']}/llm_campaign/detail?mode=llm_gen&campaign={campaign_id}")


@app.route("/manage", methods=["GET", "POST"])
@login_required
def manage():
    datasets = utils.get_local_dataset_overview(app)
    dataset_classes = list(get_dataset_classes().keys())

    datasets_enabled = {k: v for k, v in datasets.items() if v["enabled"]}
    model_outputs = utils.get_model_outputs_overview(app, datasets_enabled)

    datasets_for_download = utils.get_datasets_for_download(app)

    # set as `downloaded` the datasets that are already downloaded
    for dataset_id in datasets_for_download.keys():
        datasets_for_download[dataset_id]["downloaded"] = dataset_id in datasets

    campaigns = utils.get_sorted_campaign_list(app, sources=["crowdsourcing", "llm_eval", "llm_gen", "external"])

    return render_template(
        "manage.html",
        datasets=datasets,
        dataset_classes=dataset_classes,
        datasets_for_download=datasets_for_download,
        host_prefix=app.config["host_prefix"],
        model_outputs=model_outputs,
        campaigns=campaigns,
    )


@app.route("/save_config", methods=["POST"])
def save_config():
    data = request.get_json()
    filename = data.get("filename")
    config = data.get("config")
    mode = data.get("mode")

    if mode == "llm_eval":
        config = utils.parse_llm_eval_config(config)
    elif mode == "llm_gen":
        config = utils.parse_llm_gen_config(config)
    elif mode == "crowdsourcing":
        config = utils.parse_crowdsourcing_config(config)
    else:
        return jsonify({"error": f"Invalid mode: {mode}"})

    utils.save_config(filename, config, mode=mode)

    return utils.success()


@app.route("/save_generation_outputs", methods=["GET", "POST"])
@login_required
def save_generation_outputs():
    data = request.get_json()
    campaign_id = data.get("campaignId")
    model_name = slugify(data.get("modelName"))

    utils.save_generation_outputs(app, campaign_id, model_name)

    return utils.success()


@app.route("/submit_annotations", methods=["POST"])
def submit_annotations():
    logger.info(f"Received annotations")
    data = request.get_json()
    campaign_id = data["campaign_id"]
    annotation_set = data["annotation_set"]
    annotator_id = data["annotator_id"]
    now = int(time.time())

    save_dir = os.path.join(ANNOTATIONS_DIR, campaign_id, "files")
    os.makedirs(save_dir, exist_ok=True)
    campaign = utils.load_campaign(app, campaign_id=campaign_id, mode="crowdsourcing")

    with app.db["lock"]:
        db = campaign.db
        batch_idx = annotation_set[0]["batch_idx"]

        with open(os.path.join(save_dir, f"{batch_idx}-{annotator_id}-{now}.jsonl"), "w") as f:
            for row in annotation_set:
                f.write(json.dumps(row) + "\n")

        db.loc[db["batch_idx"] == batch_idx, "status"] = ExampleStatus.FINISHED
        db.loc[db["batch_idx"] == batch_idx, "end"] = now

        campaign.update_db(db)
        logger.info(f"Annotations for {campaign_id} (batch {batch_idx}) saved")

    return jsonify({"status": "success"})


@app.route("/set_dataset_enabled", methods=["POST"])
@login_required
def set_dataset_enabled():
    data = request.get_json()
    dataset_id = data.get("datasetId")
    enabled = data.get("enabled")

    utils.set_dataset_enabled(app, dataset_id, enabled)

    return utils.success()


@app.route("/upload_dataset", methods=["POST"])
@login_required
def upload_dataset():
    data = request.get_json()
    dataset_id = data.get("id")
    dataset_description = data.get("description")
    dataset_format = data.get("format")
    dataset_data = data.get("dataset")
    # Process each file in the dataset

    try:
        utils.upload_dataset(app, dataset_id, dataset_description, dataset_format, dataset_data)
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": f"Error while uploading dataset: {e}"})

    return utils.success()


@app.route("/upload_model_outputs", methods=["POST"])
@login_required
def upload_model_outputs():
    logger.info(f"Received model outputs")
    data = request.get_json()
    dataset_id = data["dataset"]
    split = data["split"]
    setup_id = data["setup_id"]
    model_outputs = data["outputs"]

    dataset = app.db["datasets_obj"][dataset_id]

    try:
        utils.upload_model_outputs(dataset, split, setup_id, model_outputs)
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": f"Error while adding model outputs: {e}"})

    return utils.success()
