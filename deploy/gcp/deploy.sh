#!/usr/bin/env bash
# One-shot, re-runnable deploy to Cloud Run + Firestore, modelled on utsu-ca/agm-gcp-runbook.
# Run from Google Cloud Shell in a billing-enabled project:  ./deploy/gcp/deploy.sh
# Preset any answer via env var to skip its prompt:
#   DOMAIN_NAME, PORTAL_ADMINS, ZOOM_URL, ZOOM_SSO_URL, ZOOM_REDIRECT_URIS, SMTP_URL, MAIL_FROM, ADMIN_IPS
# GCP-specific pieces live ONLY in this script. The app itself just reads PORTAL_* env vars, so to move to another
# platform/database keep the image and change PORTAL_STORE (e.g. sql:postgresql+psycopg://...); see wiki/Deployment.md.
set -euo pipefail
trap 'echo "ERROR: deploy failed at line $LINENO: $BASH_COMMAND" >&2' ERR

cd "$(dirname "$0")/../.."
REGION="${REGION:-us-central1}"
SERVICE_NAME="${SERVICE_NAME:-utsu-portal}"
SA_NAME="portal-sa"
POLICY="portal-waf"
exists() { "$@" >/dev/null 2>&1; }

ask() {  # ask VAR "Question" REGEX "hint" [default]
    local var="$1" question="$2" regex="$3" hint="$4" default="${5:-}" answer
    answer="${!var:-}"
    while :; do
        if [ -z "$answer" ]; then
            read -r -p "$question${default:+ [$default]}: " answer
            answer="${answer:-$default}"
        fi
        if [[ "$answer" =~ $regex ]]; then break; fi
        echo "  Invalid: $hint" >&2
        answer=""
    done
    printf -v "$var" '%s' "$answer"
}

command -v gcloud >/dev/null || { echo "ERROR: gcloud CLI not found (use Google Cloud Shell)." >&2; exit 1; }
PROJECT_ID="$(gcloud config get-value project 2>/dev/null)"
if [ -z "$PROJECT_ID" ] || [ "$PROJECT_ID" = "(unset)" ]; then
    echo "ERROR: no project selected. Run: gcloud config set project <PROJECT_ID>" >&2
    exit 1
fi
SA_EMAIL="${SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"
echo "Deploying to project: $PROJECT_ID in $REGION"

HOST_RE='^([a-z0-9]([a-z0-9-]*[a-z0-9])?\.)+[a-z]{2,}$'
ask DOMAIN_NAME "Website address people will visit (e.g. portal.example.com)" "$HOST_RE" "enter a hostname like portal.example.com"
ask PORTAL_ADMINS "Bootstrap admin usernames, comma-separated" '^[A-Za-z0-9._,-]+$' "letters, digits . _ - and commas" 
SMTP_URL="${SMTP_URL-}"
if [ -z "$SMTP_URL" ]; then
    read -r -p "SMTP URL for OTP email, e.g. smtps://USER:PASS@HOST:465 (blank = log only, NOT for production)" SMTP_URL
fi
MAIL_FROM="${MAIL_FROM:-no-reply@${DOMAIN_NAME#*.}}"
ZOOM_URL="${ZOOM_URL-}"; ZOOM_SSO_URL="${ZOOM_SSO_URL-}"; ZOOM_REDIRECT_URIS="${ZOOM_REDIRECT_URIS-}"
ADMIN_IPS="${ADMIN_IPS-}"  # optional: restrict /api/admin and the check-in page to these CIDRs
if [ -n "$ADMIN_IPS" ]; then
    ADMIN_IPS="$(printf '%s' "$ADMIN_IPS" | tr -d '[:space:]' | tr ',' '\n' | sed 's|^\([^/]*\)$|\1/32|' | paste -sd, -)"
fi

echo "--> Enabling GCP APIs..."
gcloud services enable run.googleapis.com compute.googleapis.com firestore.googleapis.com iam.googleapis.com \
    secretmanager.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com

if ! exists gcloud firestore databases describe --database="(default)"; then
    echo "--> Creating Firestore database..."
    gcloud firestore databases create --location="$REGION" --type=firestore-native
fi

echo "--> Setting up service account..."
if ! exists gcloud iam service-accounts describe "$SA_EMAIL"; then
    gcloud iam service-accounts create "$SA_NAME" --display-name="UTSU portal"
    echo " Sleeping for 30s for IAM consistency..."
    sleep 30
fi
gcloud projects add-iam-policy-binding "$PROJECT_ID" --member="serviceAccount:$SA_EMAIL" \
    --role="roles/datastore.user" --condition=None --quiet >/dev/null

# Secrets are generated once and never overwritten: PORTAL_SECRET peppers every uuid hash, so changing it would orphan all accounts.
ensure_secret() {  # name, generator command
    local name="$1"; shift
    if ! exists gcloud secrets describe "$name"; then
        "$@" | gcloud secrets create "$name" --data-file=- --replication-policy=automatic >/dev/null
    fi
    gcloud secrets add-iam-policy-binding "$name" --member="serviceAccount:$SA_EMAIL" \
        --role="roles/secretmanager.secretAccessor" --quiet >/dev/null
}
echo "--> Ensuring secrets..."
ensure_secret portal-secret openssl rand -hex 32
ensure_secret portal-sso-key openssl genpkey -algorithm RSA -pkeyopt rsa_key_bits:2048
SECRETS="PORTAL_SECRET=portal-secret:latest,PORTAL_SSO_KEY=portal-sso-key:latest"
if [ -n "$SMTP_URL" ]; then
    ensure_secret portal-smtp-url printf '%s' "$SMTP_URL"
    SECRETS="$SECRETS,PORTAL_SMTP_URL=portal-smtp-url:latest"
fi
if [ -n "$ZOOM_URL" ]; then
    ensure_secret portal-zoom-client-secret openssl rand -hex 24
    SECRETS="$SECRETS,PORTAL_ZOOM_CLIENT_SECRET=portal-zoom-client-secret:latest"
fi

echo "--> Deploying Cloud Run service (a few minutes)..."
ENV_VARS="^|^PORTAL_STORE=firestore|PORTAL_ADMINS=$PORTAL_ADMINS|PORTAL_BASE_URL=https://$DOMAIN_NAME|PORTAL_MAIL_FROM=$MAIL_FROM"
ENV_VARS="$ENV_VARS|PORTAL_ZOOM_URL=$ZOOM_URL|PORTAL_ZOOM_SSO_URL=$ZOOM_SSO_URL"
[ -n "$ZOOM_REDIRECT_URIS" ] && ENV_VARS="$ENV_VARS|PORTAL_ZOOM_REDIRECT_URIS=$ZOOM_REDIRECT_URIS"
# max-instances=1: realtime admin sync uses an in-process event bus (see wiki/Deployment.md to scale out).
gcloud run deploy "$SERVICE_NAME" --source . --region="$REGION" \
    --ingress=internal-and-cloud-load-balancing --allow-unauthenticated \
    --service-account="$SA_EMAIL" --min-instances=0 --max-instances="${MAX_INSTANCES:-1}" \
    --set-env-vars="$ENV_VARS" --set-secrets="$SECRETS" --quiet

echo "--> Configuring Cloud Armor..."
if ! exists gcloud compute security-policies describe "$POLICY" --global; then
    gcloud compute security-policies create "$POLICY" --description "Portal WAF" --global
fi
ensure_rule() {
    local priority="$1" description="$2"; shift 2
    if exists gcloud compute security-policies rules describe "$priority" --security-policy "$POLICY"; then
        gcloud compute security-policies rules update "$priority" --security-policy "$POLICY" "$@"
    else
        gcloud compute security-policies rules create "$priority" --security-policy "$POLICY" --description "$description" "$@"
    fi
}
ensure_rule 900 "Block SQL Injection" --action deny-403 --expression "evaluatePreconfiguredWaf('sqli-v422-stable', {'sensitivity': 1})"
ensure_rule 901 "Block XSS" --action deny-403 --expression "evaluatePreconfiguredWaf('xss-v422-stable', {'sensitivity': 1})"
if [ -n "$ADMIN_IPS" ]; then
    NOT_ALLOWED=""
    IFS=',' read -ra CIDRS <<<"$ADMIN_IPS"
    for cidr in "${CIDRS[@]}"; do NOT_ALLOWED="$NOT_ALLOWED && !inIpRange(origin.ip, '$cidr')"; done
    ensure_rule 950 "Admin surface only from allowed IPs" --action deny-403 \
        --expression "(request.path.startsWith('/api/admin') || request.path.startsWith('/static/checkin'))$NOT_ALLOWED"
fi
ensure_rule 1000 "Throttle per IP" --action throttle --src-ip-ranges "*" \
    --rate-limit-threshold-count 300 --rate-limit-threshold-interval-sec 60 \
    --conform-action allow --exceed-action deny-429 --enforce-on-key ip
gcloud compute security-policies rules update 2147483647 --security-policy "$POLICY" --action allow

echo "--> Creating load balancer..."
create_if_missing() {
    local kind="$1" name="$2"; shift 2
    # shellcheck disable=SC2086
    if ! exists gcloud compute $kind describe "$name" "${SCOPE[@]}"; then
        # shellcheck disable=SC2086
        gcloud compute $kind create "$name" "$@" "${SCOPE[@]}"
    fi
}
SCOPE=(--global)
create_if_missing addresses "${SERVICE_NAME}-ip" --network-tier=PREMIUM
IP_ADDRESS="$(gcloud compute addresses describe "${SERVICE_NAME}-ip" --global --format='value(address)')"
create_if_missing ssl-certificates "${SERVICE_NAME}-cert" --domains="$DOMAIN_NAME"
SCOPE=(--region="$REGION")
create_if_missing network-endpoint-groups "${SERVICE_NAME}-neg" --network-endpoint-type=serverless --cloud-run-service="$SERVICE_NAME"
SCOPE=(--global)
create_if_missing backend-services "${SERVICE_NAME}-backend" --load-balancing-scheme=EXTERNAL_MANAGED
gcloud compute backend-services update "${SERVICE_NAME}-backend" --global --security-policy="$POLICY"
if ! gcloud compute backend-services describe "${SERVICE_NAME}-backend" --global --format='value(backends[].group)' | grep -q "${SERVICE_NAME}-neg"; then
    gcloud compute backend-services add-backend "${SERVICE_NAME}-backend" --global \
        --network-endpoint-group="${SERVICE_NAME}-neg" --network-endpoint-group-region="$REGION"
fi
create_if_missing url-maps "${SERVICE_NAME}-url-map" --default-service "${SERVICE_NAME}-backend"
create_if_missing target-https-proxies "${SERVICE_NAME}-https-proxy" --url-map="${SERVICE_NAME}-url-map" --ssl-certificates="${SERVICE_NAME}-cert"
create_if_missing forwarding-rules "${SERVICE_NAME}-https-rule" --target-https-proxy="${SERVICE_NAME}-https-proxy" --ports=443 --address="${SERVICE_NAME}-ip"

cat <<MSG

======================================================
DEPLOYED!   https://$DOMAIN_NAME   (static IP: $IP_ADDRESS)
======================================================
NEXT STEPS:
1. Create a DNS A record: $DOMAIN_NAME -> $IP_ADDRESS (the managed certificate is issued once DNS propagates).
2. Register with PORTAL_ADMINS, sign in (the OTP is emailed), and publish/edit forms from the admin screens.
3. Zoom SSO (if used): issuer https://$DOMAIN_NAME ; discovery https://$DOMAIN_NAME/sso/.well-known/openid-configuration ;
   client id "zoom"; read the secret with:  gcloud secrets versions access latest --secret=portal-zoom-client-secret
MSG
