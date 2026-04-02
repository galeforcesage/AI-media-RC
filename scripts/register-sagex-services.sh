#!/bin/bash
# Register sagex-api-services plugin in Sage.properties
SAGE_PROPS="/opt/sagetv/server/Sage.properties"

cat >> "$SAGE_PROPS" << 'EOF'
sagetv_core_plugins/sagex-api-services/name=SageTV API Services
sagetv_core_plugins/sagex-api-services/version=9.2.8.1
sagetv_core_plugins/sagex-api-services/type=Standard
sagetv_core_plugins/sagex-api-services/impl=
sagetv_core_plugins/sagex-api-services/enabled=true
sagetv_core_plugins/sagex-api-services/installindex=7
sagetv_core_plugins/sagex-api-services/author=stuckless
sagetv_core_plugins/sagex-api-services/respath=sagex-api
sagetv_core_plugins/sagex-api-services/desc=HTTP and RMI services for SageTV
sagetv_core_plugins/sagex-api-services/desktop=false
sagetv_core_plugins/sagex-api-services/server=false
sagetv_core_plugins/sagex-api-services/dependency/sagetv/minversion=9.2.0
sagetv_core_plugins/sagex-api-services/dependency/sagetv/maxversion=
sagetv_core_plugins/sagex-api-services/dependency/sagetv/type=Core
sagetv_core_plugins/sagex-api-services/dependency/jetty/minversion=3.0.1
sagetv_core_plugins/sagex-api-services/dependency/jetty/maxversion=
sagetv_core_plugins/sagex-api-services/dependency/jetty/type=Plugin
sagetv_core_plugins/sagex-api-services/dependency/sagex-api/minversion=9.2.8.1
sagetv_core_plugins/sagex-api-services/dependency/sagex-api/maxversion=
sagetv_core_plugins/sagex-api-services/dependency/sagex-api/type=Plugin
EOF

sed -i 's/^plugin_install_counter=.*/plugin_install_counter=7/' "$SAGE_PROPS"
echo "Done. Lines with sagex-api-services:"
grep -c "sagex-api-services" "$SAGE_PROPS"
