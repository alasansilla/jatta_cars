"""Become a driver, and driver sign in, with a phone number.

The whole flow is: type your number -> type the code we text you -> (first time
only) type your name -> see your dashboard or where your application stands.

No email, no password. Joining and signing in are the same steps, because the
code is what proves who you are: a number that already has an account signs
into it, and a number that does not gets a new account. Typing a number alone
never signs anybody in.

Proving a number is not approval. A new driver is "pending" until staff approve
them, and cannot take trips before that.
"""
import re
import secrets
import time

from flask import (
    Blueprint, current_app, flash, redirect, render_template, request, session,
    url_for,
)

from . import phone_auth, sms
from .operator import _account, _current, end_session, signed_in_recently, start_session
from .phone import DEFAULT_COUNTRY_CODE, InvalidPhone, normalise, pretty

bp = Blueprint("driver_auth", __name__)

JOIN = "join"
SIGN_IN = "sign_in"
ADD = "add"


@bp.before_request
def check_csrf():
    if request.method != "POST":
        return None
    expected = session.get("_csrf_token", "")
    supplied = request.form.get("csrf_token", "")
    if not expected or not secrets.compare_digest(str(supplied), str(expected)):
        flash("This page had expired. Please try again.", "error")
        return redirect(url_for("driver_auth.sign_in"))
    return None


@bp.after_request
def private(response):
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "same-origin"
    return response


def _ip():
    return request.remote_addr or "unknown"


def _landing(intent):
    return url_for("driver_auth.join" if intent == JOIN else "driver_auth.sign_in")


def _after_sign_in(driver):
    if driver.is_approved:
        return redirect(url_for("operator.dashboard"))
    return redirect(url_for("driver_auth.status"))


def _phone_page(intent, error=None, country_code=DEFAULT_COUNTRY_CODE, number="",
                status=200):
    template = {JOIN: "driver/join.html", SIGN_IN: "driver/sign_in.html",
                ADD: "driver/phone.html"}[intent]
    return render_template(
        template, intent=intent, error=error, country_code=country_code,
        number=number, sms_ready=sms.available(), account=_account(),
    ), status


@bp.get("/join")
def join():
    if _account() is not None:
        return _after_sign_in(_account())
    return _phone_page(JOIN)


@bp.get("/sign-in")
def sign_in():
    if _account() is not None:
        return _after_sign_in(_account())
    return _phone_page(SIGN_IN)


@bp.post("/send-code")
def send_code():
    intent = request.form.get("intent")
    if intent not in (JOIN, SIGN_IN, ADD):
        intent = SIGN_IN
    country_code = str(request.form.get("country_code") or DEFAULT_COUNTRY_CODE)[:6]
    number = str(request.form.get("phone") or "")[:32]

    account = _account()
    if intent == ADD:
        if account is None:
            return redirect(url_for("driver_auth.sign_in"))
        if not signed_in_recently(current_app.config["RECENT_SIGN_IN_SECONDS"]):
            # Adding or changing a sign-in number needs a fresh sign-in, so an old
            # or borrowed session cannot quietly attach someone else's phone.
            had_phone = bool(account.phone_e164)
            end_session()
            flash("To add or change your number, please sign in again first.", "error")
            return redirect(url_for("driver_auth.sign_in" if had_phone else "operator.login"))
    elif account is not None:
        return _after_sign_in(account)

    try:
        phone = normalise(country_code, number)
    except InvalidPhone as error:
        return _phone_page(intent, str(error), country_code, number, status=400)

    if intent == ADD and phone == account.phone_e164:
        return _phone_page(intent, "That is already your number.", country_code, number,
                           status=400)
    if intent == ADD and phone_auth.driver_for_phone(phone) is not None:
        # Don't text a code to a number another driver signs in with: it could
        # never be attached, and it would only pester that driver.
        return _phone_page(intent, "That number is already used by another driver account. "
                                   f"If it is yours, please contact {phone_auth.brand()}.",
                           country_code, number, status=409)

    purpose = phone_auth.ADD_PHONE if intent == ADD else phone_auth.SIGN_IN
    nonce = phone_auth.new_nonce()
    try:
        phone_auth.request_code(phone, purpose, nonce, _ip())
    except phone_auth.Refused as error:
        return _phone_page(intent, str(error), country_code, number,
                           status=429 if error.retry_after else 400)

    session["phone_flow"] = {"phone": phone, "purpose": purpose, "intent": intent,
                             "account_id": account.id if intent == ADD else None,
                             "nonce": nonce, "started": int(time.time())}
    return redirect(url_for("driver_auth.code"))


def _flow():
    flow = session.get("phone_flow")
    if not isinstance(flow, dict) or not flow.get("phone") or not flow.get("nonce"):
        return None
    # A flow older than the code itself can never succeed; start again cleanly.
    if time.time() - int(flow.get("started", 0)) > current_app.config["SMS_CODE_TTL_SECONDS"] + 3600:
        session.pop("phone_flow", None)
        return None
    return flow


def _code_page(flow, error=None, status=200):
    return render_template(
        "driver/code.html", flow=flow, phone=pretty(flow["phone"]), error=error,
        wait=phone_auth.resend_wait(flow["phone"], flow["purpose"]),
        fake_sms=sms.is_fake(),
    ), status


@bp.route("/code", methods=["GET", "POST"])
def code():
    flow = _flow()
    if flow is None:
        flash("Please enter your phone number to get a code.", "error")
        return redirect(url_for("driver_auth.sign_in"))
    if request.method == "GET":
        return _code_page(flow)

    try:
        phone_auth.verify_code(flow["phone"], flow["purpose"], flow["nonce"],
                               request.form.get("code"), _ip())
    except phone_auth.Refused as error:
        return _code_page(flow, str(error), status=400)

    session.pop("phone_flow", None)
    phone = flow["phone"]

    if flow["purpose"] == phone_auth.ADD_PHONE:
        account = _account()
        if account is None or account.id != flow.get("account_id") or not signed_in_recently(
                current_app.config["RECENT_SIGN_IN_SECONDS"]):
            return redirect(url_for("driver_auth.sign_in"))
        result = phone_auth.attach_phone(account.id, phone)
        if result == "taken":
            flash("That number is already used by another driver account, so it was "
                  f"not added. If it is yours, please contact {phone_auth.brand()}.", "error")
        elif result == "attached":
            flash(f"Your number {pretty(phone)} is saved. You can now sign in with it.",
                  "success")
        return _after_sign_in(account)

    driver = phone_auth.driver_for_phone(phone)
    if driver is not None:
        start_session(driver, "phone")
        flash("You are signed in.", "success")
        return _after_sign_in(driver)

    session["phone_verified"] = {"phone": phone, "at": int(time.time()),
                                 "intent": flow.get("intent")}
    return redirect(url_for("driver_auth.name"))


@bp.post("/code/resend")
def resend():
    flow = _flow()
    if flow is None:
        return redirect(url_for("driver_auth.sign_in"))
    try:
        phone_auth.request_code(flow["phone"], flow["purpose"], flow["nonce"], _ip())
    except phone_auth.Refused as error:
        return _code_page(flow, str(error), status=429 if error.retry_after else 400)
    flow["started"] = int(time.time())
    session["phone_flow"] = flow
    flash("We sent you a new code.", "success")
    return redirect(url_for("driver_auth.code"))


@bp.get("/change-number")
def change_number():
    flow = session.pop("phone_flow", None) or {}
    if flow.get("intent") == ADD:
        return redirect(url_for("driver_auth.phone"))
    return redirect(_landing(flow.get("intent")))


def _clean_name(raw):
    name = re.sub(r"\s+", " ", str(raw or "")).strip()
    if len(name) < 2 or len(name) > 80 or not any(ch.isalpha() for ch in name):
        return None
    return name


@bp.route("/name", methods=["GET", "POST"])
def name():
    verified = session.get("phone_verified")
    fresh = isinstance(verified, dict) and verified.get("phone") and \
        time.time() - int(verified.get("at", 0)) <= current_app.config["PHONE_VERIFIED_SECONDS"]
    if not fresh:
        session.pop("phone_verified", None)
        flash("Please confirm your phone number again.", "error")
        return redirect(url_for("driver_auth.join"))

    phone = verified["phone"]
    existing = phone_auth.driver_for_phone(phone)
    if existing is not None:
        session.pop("phone_verified", None)
        start_session(existing, "phone")
        return _after_sign_in(existing)

    if phone_auth.unverified_claims(phone):
        return render_template("driver/name.html", phone=pretty(phone), blocked=True,
                               error=None, value="")

    error, value = None, ""
    if request.method == "POST":
        value = str(request.form.get("name") or "")[:120]
        cleaned = _clean_name(value)
        if cleaned is None:
            error = "Please type your name."
        else:
            driver, _created = phone_auth.find_or_create_driver(phone, cleaned)
            session.pop("phone_verified", None)
            start_session(driver, "phone")
            return _after_sign_in(driver)

    return render_template("driver/name.html", phone=pretty(phone), blocked=False,
                           error=error, value=value), (400 if error else 200)


@bp.get("/status")
def status():
    account = _account()
    if account is None:
        return redirect(url_for("driver_auth.sign_in"))
    if account.is_approved:
        return redirect(url_for("operator.dashboard"))
    return render_template("driver/status.html", account=account,
                           phone=pretty(account.phone_e164))


@bp.get("/phone")
def phone():
    account = _account()
    if account is None:
        return redirect(url_for("driver_auth.sign_in"))
    if not signed_in_recently(current_app.config["RECENT_SIGN_IN_SECONDS"]):
        end_session()
        return redirect(url_for("driver_auth.sign_in"))
    return _phone_page(ADD)


@bp.get("/sign-out")
def sign_out():
    end_session()
    flash("You are signed out.", "success")
    return redirect(url_for("public.index"))
