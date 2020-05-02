import os
import sys
import re
import random
import subprocess
import itertools
import yaml

GENERATED = ".cleat"

TEMPLATE_PORT_LISTEN = """\
    listen << PORT_80_443 >>;
    server_name << DOMAIN_NAME >>;
"""

TEMPLATE_WELLKNOWN_LOCATION = """\
    location /.well-known/ {
        root /usr/share/nginx/<< DOMAIN_NAME >>/;
    }
"""

TEMPLATE_SSL_CONFIG = """\
    add_header Strict-Transport-Security max-age=31536000;

    ssl_certificate << CLEAT_ROOT >>/chained-<< DOMAIN_NAME >>.pem;
    ssl_certificate_key << CLEAT_ROOT >>/<< DOMAIN_NAME >>.key;
    ssl_session_timeout 5m;
    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_ciphers "ECDHE-ECDSA-AES128-GCM-SHA256:ECDHE-ECDSA-AES256-GCM-SHA384:ECDHE-RSA-AES128-GCM-SHA256:ECDHE-RSA-AES256-GCM-SHA384:ECDHE-ECDSA-AES128-SHA:ECDHE-ECDSA-AES256-SHA:ECDHE-ECDSA-AES128-SHA256:ECDHE-ECDSA-AES256-SHA384:ECDHE-RSA-AES128-SHA:ECDHE-RSA-AES256-SHA:ECDHE-RSA-AES128-SHA256:ECDHE-RSA-AES256-SHA384:DHE-RSA-AES128-GCM-SHA256:DHE-RSA-AES256-GCM-SHA384:DHE-RSA-AES128-SHA:DHE-RSA-AES256-SHA:DHE-RSA-AES128-SHA256:DHE-RSA-AES256-SHA256";
    ssl_session_cache shared:SSL:50m;
    ssl_dhparam << CLEAT_ROOT >>/dhparam4096.pem;
    ssl_prefer_server_ciphers on;
"""

TEMPLATE_LOCATION_CHUNK = """\
    location /<< LOCATION >> {
        proxy_set_header    Host $host;
        proxy_set_header    X-Real-IP $remote_addr;
        proxy_set_header    X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header    X-Forwarded-Proto $scheme;

        proxy_pass          http://<< HOSTNAME >>:<< PORT >>;
        << REWRITE >>
        proxy_read_timeout  90;
    }
"""


def _templated(template, site, path=None, **kwargs):
    values = {k.upper(): value for k, value in kwargs.items()}

    values["DOMAIN_NAME"] = site
    values["CLEAT_ROOT"] = "/etc/nginx/cleat"
    if path != None:
        segs = path[0].split("/", 1)
        if len(segs) == 1:
            domain, location = segs[0], ""
        else:
            domain, location = segs
        values["LOCATION"] = location
        values["PORT"] = path[1].get("port", 80)

    def replace(match):
        mgupper = match.group(1)
        if mgupper in values:
            return str(values[mgupper])
        print(f"Template match: {match.group(1)} not found", file=sys.stderr)
        return match.group(0)

    myrepls = re.sub("<< ([_A-Z0-9]+) >>", replace, template)
    return myrepls


def grouped_sites(config):
    domain = lambda url: url.split("/", 1)[0]

    sortlist = sorted(config.items())
    grouped = itertools.groupby(sortlist, key=lambda pair: domain(pair[0]))
    yield from grouped


def generate_configuration(filename, ssl=True, plain=False):
    configdir = os.path.dirname(os.path.realpath(filename))

    with open(filename, "r") as stream:
        config = yaml.safe_load(stream)
    confdir = os.path.join(configdir, GENERATED, "nginx", "conf.d")
    httpsdir = os.path.join(configdir, GENERATED, "https")

    for site, paths in grouped_sites(config):
        port80_server = []
        port443_server = []

        port80_server.append(_templated(TEMPLATE_PORT_LISTEN, site, port_80_443=80))

        port443_server.append(
            _templated(TEMPLATE_PORT_LISTEN, site, port_80_443="443 ssl")
        )
        port443_server.append(_templated(TEMPLATE_SSL_CONFIG, site))

        for path in paths:
            hostname = "cleat-" + re.sub("[^a-zA-Z0-9]", "_", path[0])

            url = path[0]
            domain, basepath = url.split("/", 1) if "/" in url else (url, None)

            if basepath not in [None, ""]:
                rewrite = f"rewrite /{basepath}/(.*) /$1  break;"
            else:
                rewrite = ""

            if plain:
                port80_server.append(
                    _templated(
                        TEMPLATE_LOCATION_CHUNK,
                        site,
                        path=path,
                        hostname=hostname,
                        rewrite=rewrite,
                    )
                )
            port443_server.append(
                _templated(
                    TEMPLATE_LOCATION_CHUNK,
                    site,
                    path=path,
                    hostname=hostname,
                    rewrite=rewrite,
                )
            )

        port80_server = ["server {"] + port80_server + ["}\n"]
        port443_server = ["server {"] + port443_server + ["}\n"]

        if not os.path.exists(confdir):
            os.makedirs(confdir)
        outfile_site = os.path.join(confdir, site + ".conf")
        with open(outfile_site, "w") as conf:
            conf.write("\n".join(port80_server))
            if ssl:
                conf.write("\n".join(port443_server))


def generate_configuration_acme(filename):
    configdir = os.path.dirname(os.path.realpath(filename))

    with open(filename, "r") as stream:
        config = yaml.safe_load(stream)

    for site, paths in grouped_sites(config):
        port80_server = []

        port80_server.append(_templated(TEMPLATE_PORT_LISTEN, site, port_80_443=80))
        port80_server.append(_templated(TEMPLATE_WELLKNOWN_LOCATION, site))

        port80_server = ["server {"] + port80_server + ["}\n"]

        confdir = os.path.join(configdir, GENERATED, "nginx-acme")
        if not os.path.exists(confdir):
            os.makedirs(confdir)
        outfile_site = os.path.join(confdir, site + ".conf")
        with open(outfile_site, "w") as conf:
            conf.write("\n".join(port80_server))


TEMPLATE_SERVICE = """
[Unit]
Description=<< DOCKER_DESCR >>
Requires=docker.service
After=docker.service

[Service]
Restart=always
ExecStart=/usr/bin/docker start -a << DOCKER_TAG >>
ExecStop=/usr/bin/docker stop -t 10 << DOCKER_TAG >>

[Install]
WantedBy=local.target
"""


def generate_systemd_services():

    pass


def initialize_https(filename):
    singleton_script = """
openssl genrsa 4096 > account.key
openssl dhparam -out dhparam4096.pem 4096
"""

    with open(filename, "r") as stream:
        config = yaml.safe_load(stream)
    configdir = os.path.dirname(os.path.realpath(filename))
    httpsdir = os.path.join(configdir, GENERATED, "https")

    if not os.path.exists(httpsdir):
        os.mkdir(httpsdir)
    os.chdir(httpsdir)

    base_file_exists = lambda fn: os.path.exists(os.path.join(fn))
    if not base_file_exists("account.key") and not base_file_exists("dhparam4096.pem"):
        subprocess.run(singleton_script, shell=True)

    gen_key_script = """
openssl genrsa 4096 > << DOMAIN_NAME >>.key
openssl req \
        -new \
        -sha256 \
        -key << DOMAIN_NAME >>.key \
        -subj "/" \
        -reqexts SAN \
        -config <(cat /etc/ssl/openssl.cnf <(printf "[SAN]\nsubjectAltName=DNS:<< DOMAIN_NAME >>")) \
        > << DOMAIN_NAME >>.csr
"""

    for site, paths in grouped_sites(config):
        print(site)
        gkey = _templated(gen_key_script, site)
        subprocess.run(gkey, shell=True, executable="/bin/bash")

    curl_cross = "curl https://letsencrypt.org/certs/lets-encrypt-x3-cross-signed.pem -o ./lets-encrypt-x3-cross-signed.pem"
    subprocess.run(curl_cross, shell=True)


def _start_acme_server(confdir, httpsdir):
    alpha = "abcdefghijklmnopqrstuvwxyz0123456789"
    runname = "".join([random.choice(alpha) for x in range(8)])

    args = [
        "docker",
        "run",
        "--rm",
        "-d",
        "--name",
        "cleat-nginx-server",
        "-p",
        "80:80",
        "-l",
        runname,
        "-v",
        f"{confdir}:/etc/nginx/conf.d",
        "-v",
        f"{httpsdir}:/usr/share/nginx/",
        "nginx",
    ]

    # print(" ".join(args))
    subprocess.run(args)
    return runname


def _stop_acme_server(runname):
    cmd = f'docker stop `docker ps --filter "label={runname}" -q `'
    # print(cmd)
    subprocess.run(cmd, shell=True)


def refresh_https(filename):
    re_up_script = """
python << ACME_DIR >>/acme_tiny.py \
        --account-key ./account.key \
        --csr ./<< DOMAIN_NAME >>.csr \
        --acme-dir << HTTPSDIR >>/<< DOMAIN_NAME >>/.well-known/acme-challenge \
                > ./signed-<< DOMAIN_NAME >>.crt
cat signed-<< DOMAIN_NAME >>.crt lets-encrypt-x3-cross-signed.pem > chained-<< DOMAIN_NAME >>.pem
"""

    with open(filename, "r") as stream:
        config = yaml.safe_load(stream)
    configdir = os.path.dirname(os.path.realpath(filename))
    httpsdir = os.path.join(configdir, GENERATED, "https")
    confdir = os.path.join(configdir, GENERATED, "nginx-acme")

    x1 = os.path.abspath(__file__)
    x2 = os.path.dirname(x1)
    x3 = os.path.dirname(x2)
    acmedir = os.path.join(x3, "acme")

    if not os.path.exists(httpsdir):
        os.mkdir(httpsdir)
    os.chdir(httpsdir)

    runname = _start_acme_server(confdir, httpsdir)

    for site, paths in grouped_sites(config):
        well_known = os.path.join(httpsdir, site, ".well-known", "acme-challenge")
        if not os.path.exists(well_known):
            os.makedirs(well_known)
        gkey = _templated(re_up_script, site, acme_dir=acmedir, httpsdir=httpsdir)
        subprocess.run(gkey, shell=True)

    _stop_acme_server(runname)


def restart(service):
    os.system("service << >> stop")
    os.system("service << >> start")


def run_server(filename):
    # run all the dockers in a non-ssl env for testing

    configdir = os.path.dirname(os.path.realpath(filename))

    with open(filename, "r") as stream:
        config = yaml.safe_load(stream)
    httpsdir = os.path.join(configdir, GENERATED, "https")
    confdir = os.path.join(configdir, GENERATED, "nginx", "conf.d")

    alpha = "abcdefghijklmnopqrstuvwxyz0123456789"
    runname = "".join([random.choice(alpha) for x in range(8)])

    args = ["docker", "network", "create", f"cleat_{runname}"]
    # print(" ".join(args))
    subprocess.run(args)

    for url, siteconfig in config.items():
        # run a docker container for each backing server

        name = "cleat-" + re.sub("[^a-zA-Z0-9]", "_", url)

        args = [
            "docker",
            "run",
            "--rm",
            "-d",
            "-l",
            runname,
            "--name",
            name,
            "--hostname",
            name,
            "--network",
            f"cleat_{runname}",
            siteconfig["image"],
        ]
        # print(" ".join(args))

        subprocess.run(args)

    args = [
        "docker",
        "run",
        "--rm",
        "-d",
        "--name",
        "cleat-nginx-server",
        "-p",
        "80:80",
        "-p",
        "443:443",
        "--network",
        f"cleat_{runname}",
        "-l",
        runname,
        "-v",
        f"{httpsdir}:/etc/nginx/cleat",
        "-v",
        f"{confdir}:/etc/nginx/conf.d",
        "nginx",
    ]

    # print(" ".join(args))
    subprocess.run(args)

    print("services running:  stop with\n" f"cleat stop {runname}")


def stop_server(runname):
    cmd = f'docker stop `docker ps --filter "label={runname}" -q `'
    # print(cmd)
    subprocess.run(cmd, shell=True)

    args = ["docker", "network", "remove", f"cleat_{runname}"]
    # print(" ".join(args))
    subprocess.run(args)