-- Development-only service credentials; production roles use separate secret files.
CREATE ROLE ingress LOGIN PASSWORD 'ingress-dev';
CREATE DATABASE ingress OWNER ingress;
REVOKE CONNECT ON DATABASE ingress FROM PUBLIC;
GRANT CONNECT ON DATABASE ingress TO ingress;

CREATE ROLE orchestrator LOGIN PASSWORD 'orchestrator-dev';
CREATE DATABASE orchestrator OWNER orchestrator;
REVOKE CONNECT ON DATABASE orchestrator FROM PUBLIC;
GRANT CONNECT ON DATABASE orchestrator TO orchestrator;

CREATE ROLE funding LOGIN PASSWORD 'funding-dev';
CREATE DATABASE funding OWNER funding;
REVOKE CONNECT ON DATABASE funding FROM PUBLIC;
GRANT CONNECT ON DATABASE funding TO funding;

CREATE ROLE publisher LOGIN PASSWORD 'publisher-dev';
CREATE DATABASE publisher OWNER publisher;
REVOKE CONNECT ON DATABASE publisher FROM PUBLIC;
GRANT CONNECT ON DATABASE publisher TO publisher;

CREATE ROLE exchange LOGIN PASSWORD 'exchange-dev';
CREATE DATABASE exchange OWNER exchange;
REVOKE CONNECT ON DATABASE exchange FROM PUBLIC;
GRANT CONNECT ON DATABASE exchange TO exchange;

CREATE ROLE gateway LOGIN PASSWORD 'gateway-dev';
CREATE DATABASE gateway OWNER gateway;
REVOKE CONNECT ON DATABASE gateway FROM PUBLIC;
GRANT CONNECT ON DATABASE gateway TO gateway;

CREATE ROLE backoffice LOGIN PASSWORD 'backoffice-dev';
CREATE DATABASE backoffice OWNER backoffice;
REVOKE CONNECT ON DATABASE backoffice FROM PUBLIC;
GRANT CONNECT ON DATABASE backoffice TO backoffice;
