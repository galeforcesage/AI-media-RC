#!/bin/bash
# Register Jetty and dependency plugins in Sage.properties
SAGE_PROPS="/opt/sagetv/server/Sage.properties"

# Update plugin_install_counter
sed -i 's/^plugin_install_counter=.*/plugin_install_counter=6/' "$SAGE_PROPS"

# Remove the load_at_startup_runnable_classes line we added earlier (plugin manager handles this)
sed -i '/^load_at_startup_runnable_classes=sagex.jetty.starter.JettyPlugin/d' "$SAGE_PROPS"

# Remove jetty/port if already there (Jetty manages its own default)
sed -i '/^jetty\/port=8080/d' "$SAGE_PROPS"

# Append all plugin registration properties
cat >> "$SAGE_PROPS" << 'EOF'
sagetv_core_plugins/sagex-api/name=SageTV API Wrappers
sagetv_core_plugins/sagex-api/version=9.2.8.1
sagetv_core_plugins/sagex-api/type=Library
sagetv_core_plugins/sagex-api/impl=
sagetv_core_plugins/sagex-api/enabled=true
sagetv_core_plugins/sagex-api/installindex=1
sagetv_core_plugins/sagex-api/author=stuckless
sagetv_core_plugins/sagex-api/respath=sagex-api
sagetv_core_plugins/sagex-api/desc=SageTV API Wrappers with Remote API capabilities
sagetv_core_plugins/sagex-api/desktop=false
sagetv_core_plugins/sagex-api/server=false
sagetv_core_plugins/ant/name=Apache Ant
sagetv_core_plugins/ant/version=1.8.2.1
sagetv_core_plugins/ant/type=Library
sagetv_core_plugins/ant/impl=
sagetv_core_plugins/ant/enabled=true
sagetv_core_plugins/ant/installindex=2
sagetv_core_plugins/ant/author=Apache
sagetv_core_plugins/ant/respath=ant
sagetv_core_plugins/ant/desc=Apache Ant Library
sagetv_core_plugins/ant/desktop=false
sagetv_core_plugins/ant/server=false
sagetv_core_plugins/slf4j-api/name=SLF4J API
sagetv_core_plugins/slf4j-api/version=1.7.12
sagetv_core_plugins/slf4j-api/type=Library
sagetv_core_plugins/slf4j-api/impl=
sagetv_core_plugins/slf4j-api/enabled=true
sagetv_core_plugins/slf4j-api/installindex=3
sagetv_core_plugins/slf4j-api/author=QOS.ch
sagetv_core_plugins/slf4j-api/respath=slf4j-api
sagetv_core_plugins/slf4j-api/desc=SLF4J API Module
sagetv_core_plugins/slf4j-api/desktop=false
sagetv_core_plugins/slf4j-api/server=false
sagetv_core_plugins/slf4j-log4j12/name=SLF4J Log4j12 Binding
sagetv_core_plugins/slf4j-log4j12/version=1.7.12
sagetv_core_plugins/slf4j-log4j12/type=Library
sagetv_core_plugins/slf4j-log4j12/impl=
sagetv_core_plugins/slf4j-log4j12/enabled=true
sagetv_core_plugins/slf4j-log4j12/installindex=4
sagetv_core_plugins/slf4j-log4j12/author=QOS.ch
sagetv_core_plugins/slf4j-log4j12/respath=slf4j-log4j12
sagetv_core_plugins/slf4j-log4j12/desc=SLF4J Log4j12 Binding
sagetv_core_plugins/slf4j-log4j12/desktop=false
sagetv_core_plugins/slf4j-log4j12/server=false
sagetv_core_plugins/log4j/name=Log4j
sagetv_core_plugins/log4j/version=1.2.17
sagetv_core_plugins/log4j/type=Library
sagetv_core_plugins/log4j/impl=
sagetv_core_plugins/log4j/enabled=true
sagetv_core_plugins/log4j/installindex=5
sagetv_core_plugins/log4j/author=Apache
sagetv_core_plugins/log4j/respath=log4j
sagetv_core_plugins/log4j/desc=Apache Log4j
sagetv_core_plugins/log4j/desktop=false
sagetv_core_plugins/log4j/server=false
sagetv_core_plugins/jetty/name=Jetty Web Server
sagetv_core_plugins/jetty/version=3.0.3.250
sagetv_core_plugins/jetty/type=Standard
sagetv_core_plugins/jetty/impl=sagex.jetty.starter.JettyPlugin
sagetv_core_plugins/jetty/enabled=true
sagetv_core_plugins/jetty/installindex=6
sagetv_core_plugins/jetty/author=jreichen, jusjoken
sagetv_core_plugins/jetty/respath=jetty
sagetv_core_plugins/jetty/desc=Provides a platform for SageTV web application plugins
sagetv_core_plugins/jetty/desktop=false
sagetv_core_plugins/jetty/server=false
sagetv_core_plugins/jetty/dependency/sagetv/minversion=9.0.0
sagetv_core_plugins/jetty/dependency/sagetv/maxversion=
sagetv_core_plugins/jetty/dependency/sagetv/type=Core
sagetv_core_plugins/jetty/dependency/java/minversion=1.8
sagetv_core_plugins/jetty/dependency/java/maxversion=
sagetv_core_plugins/jetty/dependency/java/type=JVM
sagetv_core_plugins/jetty/dependency/sagex-api/minversion=9.1.7.0
sagetv_core_plugins/jetty/dependency/sagex-api/maxversion=
sagetv_core_plugins/jetty/dependency/sagex-api/type=Plugin
sagetv_core_plugins/jetty/dependency/ant/minversion=1.8.2.1
sagetv_core_plugins/jetty/dependency/ant/maxversion=
sagetv_core_plugins/jetty/dependency/ant/type=Plugin
sagetv_core_plugins/jetty/dependency/slf4j-api/minversion=1.7.12
sagetv_core_plugins/jetty/dependency/slf4j-api/maxversion=
sagetv_core_plugins/jetty/dependency/slf4j-api/type=Plugin
sagetv_core_plugins/jetty/dependency/slf4j-log4j12/minversion=1.7.12
sagetv_core_plugins/jetty/dependency/slf4j-log4j12/maxversion=
sagetv_core_plugins/jetty/dependency/slf4j-log4j12/type=Plugin
sagetv_core_plugins/jetty/dependency/log4j/minversion=1.2.17
sagetv_core_plugins/jetty/dependency/log4j/maxversion=
sagetv_core_plugins/jetty/dependency/log4j/type=Plugin
EOF

echo "Plugin registration complete."
grep -c "sagetv_core_plugins" "$SAGE_PROPS"
