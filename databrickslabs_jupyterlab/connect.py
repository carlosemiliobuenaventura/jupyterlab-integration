import datetime
import glob
import io
import os
import random
import sys
import threading
import uuid
import warnings

from IPython import get_ipython
from IPython.display import HTML, display
from IPython.core.magic import Magics, magics_class, line_cell_magic

from databrickslabs_jupyterlab.rest import Command
from databrickslabs_jupyterlab.progress import load_progressbar, debug
from databrickslabs_jupyterlab.dbfs import Dbfs
from databrickslabs_jupyterlab.database import Databases


py4j = glob.glob("/databricks/spark/python/lib/py4j-*-src.zip")[0]
sys.path.insert(0, py4j)
sys.path.insert(0, "/databricks/spark/python")
sys.path.insert(0, "/databricks/jars/spark--driver--spark--resources-resources.jar")

from pyspark.conf import SparkConf  # pylint: disable=import-error,wrong-import-position

with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    # Suppress py4j loading message on stderr by redirecting sys.stderr
    stderr_orig = sys.stderr
    sys.stderr = io.StringIO()
    from PythonShell import get_existing_gateway, RemoteContext  # pylint: disable=import-error

    out = sys.stderr.getvalue()
    # Restore sys.stderr
    sys.stderr = stderr_orig
    # Print any other error message to stderr
    if not "py4j imported" in out:
        print(out, file=sys.stderr)

from dbutils import DBUtils  # pylint: disable=import-error,wrong-import-position


class JobInfo:
    """Job info class for Spark jobs
    Args:
        sc (SparkContext): Spark Context
    """

    def __init__(self, sc):
        self.pool_id = str(random.getrandbits(64))
        self.group_id = "jupyterlab-default-group"
        self.is_running = {}
        self.current_thread = None
        self.sc = sc

    def dump(self, tag):
        if debug():
            print(
                tag,
                "%s (%s)"
                % (
                    datetime.datetime.now().isoformat(),
                    threading.current_thread().__class__.__name__,
                ),
            )
            print("%s: pool_id              %s" % (tag, self.pool_id))
            print("%s: group_id             %s" % (tag, self.group_id))
            print(
                "%s: spark.scheduler.pool %s"
                % (tag, self.sc.getLocalProperty("spark.scheduler.pool"))
            )
            print(
                "%s: spark.jobGroup.id    %s" % (tag, self.sc.getLocalProperty("spark.jobGroup.id"))
            )
            print("%s: running              %s" % (tag, self.is_running.get(self.group_id, None)))

    def attach(self):
        if debug():
            print(
                "\nATTACHING to ",
                self.group_id,
                "in",
                threading.current_thread().__class__.__name__,
            )
        # Catch [SPARK-22340][PYTHON] Add a mode to pin Python thread into JVM's
        # This code explicitely attaches both python threads to the value Spark Context is set to
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            self.sc.setLocalProperty("spark.scheduler.pool", self.pool_id)

        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            self.sc.setJobGroup(self.group_id, "jupyterlab job group", True)

    def stop_all(self):
        if debug():
            print("\nSTOPPING all running threads")
        for k, v in self.is_running.items():
            if v:
                print(k, v)
            self.is_running[k] = False

    def new_group_id(self):
        self.group_id = self.pool_id + "_" + uuid.uuid4().hex
        self.attach()


class DbjlUtils:
    def __init__(self, shell, entry_point):
        self._dbutils = DBUtils(shell, entry_point)
        self.fs = self._dbutils.fs
        self.secrets = self._dbutils.secrets

    def help(self):
        html = """
        This module provides a subset of the DBUtils tools working for Jupyterlab Integration
        <br/><br/>
        <b>fs: DbfsUtils</b> -&gt; Manipulates the Databricks filesystem (DBFS) from the console
        <br/>
        <b>secrets: SecretUtils</b> -&gt; Provides utilities for leveraging secrets within notebooks
        """
        display(HTML(html))


@magics_class
class DbjlMagics(Magics):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.url = os.environ["DBJL_HOST"]
        self.cluster = os.environ["DBJL_CLUSTER"]
        self.scope = None
        self.key = None
        self.command = None

    @line_cell_magic
    def scala(self, line, cell=None):
        "Magic that allows to create scala context and execute scala in a notebook"
        if cell is None:
            sline = line.strip(" ")

            if sline in ["-s", "--stop"] and self.command:
                self.command.close()
                print("Scala execution context closed")
                return

            if sline == "":
                args = []
            else:
                args = sline.split(" ")

            if len(args) == 0:
                scope = "dbjl_creds"
                key = os.environ["DBJL_PROFILE"]
            elif len(args) == 2:
                scope, key = args
            else:
                print("Error: Either no parameter or two (scope, key)")

            try:
                self.scope = scope
                self.key = key
                dbutils = get_ipython().user_ns["dbutils"]
                pat = dbutils.secrets.get(scope, key)
            except Exception as ex:  # pylint: disable=broad-except
                self.scope = None
                self.key = None
                print("Error: Couldn't retrieve secret\n", str(ex))
                return

            try:
                self.command = Command(
                    url=self.url, cluster_id=self.cluster, token=pat, language="scala"
                )
                print("Scala execution context created")
                del pat
            except Exception as ex:  # pylint: disable=broad-except
                self.command = None
                print("Error: Couldn't create Scala execution context\n", str(ex))
                return
        else:
            try:
                result = self.command.execute(cell)
            except Exception as ex:  # pylint: disable=broad-except
                result = (-1, str(ex))

            if result[0] == 0:
                print(result[1])
            else:
                print("Error: " + result[1])

    @line_cell_magic
    def sql(self, line, cell=None):
        """Cell magic th execute SQL commands

        Args:
            line (str): line behind %sql
            cell (str, optional): cell below %sql. Defaults to None.
        
        Returns:
            DataFrame: DataFrame of the SQL result
        """
        ip = get_ipython()
        spark = ip.user_ns["spark"]
        if cell is None:
            code = line
        else:
            code = cell
        return spark.sql(code)


class DatabricksBrowser:
    """[summary]
    Args:
        spark (SparkSession): Spark Session object
        dbutils (DBUtils): DbUtils object
    """

    def __init__(self, spark, dbutils):
        self.spark = spark
        self.dbutils = dbutils

    def dbfs(self, rows=30, path="/"):
        """Start dbfs browser"""
        Dbfs(self.dbutils).create(rows, path)

    def databases(self):
        """Start Database browser"""
        Databases(self.spark).create()


def dbcontext(progressbar=True, gw_port=None, gw_token=None):
    """Create a databricks context
    The following objects will be created
    - Spark Session
    - Spark Context
    - Spark Hive Context
    - DBUtils (fs module only)
    
    Args:
        progressbar (bool, optional): If True spark progressbars will be shown. Default: True.
    """

    def get_sparkui_url(host, organisation, clusterId):
        if organisation is None:
            sparkUi = "%s#/setting/clusters/%s/sparkUi" % (host, clusterId)
        else:
            sparkUi = "%s/?o=%s#/setting/clusters/%s/sparkUi" % (host, organisation, clusterId)
        return sparkUi

    def show_status(spark):
        output = """
        <div>
            <dl>
            <dt>Spark Version</dt><dd>{sc.version}</dd>
            <dt>Spark Application</dt><dd>{sc.appName}</dd>
            <dt>Spark UI</dt><dd><a href="{sparkUi}">go to ...</a></dd>
            </dl>
        </div>
        """.format(
            sc=spark.sparkContext,
            sparkUi=get_sparkui_url(host, organisation, clusterId),
            # num_executors=len(spark.sparkContext._jsc.sc().statusTracker().getExecutorInfos()),
        )
        display(HTML(output))

    # Get the configuration injected by the client
    #
    profile = os.environ.get("DBJL_PROFILE", None)
    host = os.environ.get("DBJL_HOST", None)
    clusterId = os.environ.get("DBJL_CLUSTER", None)
    organisation = os.environ.get("DBJL_ORG", None)

    print(
        "profile={}, organisation={}. cluster_id={}, host={}".format(
            profile, organisation, clusterId, host
        )
    )
    sparkUi = get_sparkui_url(host, organisation, clusterId)

    print("Spark UI = {}".format(sparkUi))

    ip = get_ipython()

    print("Connect: Gateway port = {}, token = {}".format(gw_port, gw_token))

    interpreter = "/databricks/python/bin/python"
    # Ensure that driver and executors use the same python
    #
    os.environ["PYSPARK_PYTHON"] = interpreter
    os.environ["PYSPARK_DRIVER_PYTHON"] = interpreter

    # ... and connect to this gateway
    #
    try:
        # up to DBR version 6.4
        gateway = get_existing_gateway(gw_port, True, gw_token)
    except TypeError:
        # for DBR 6.5 and higher
        gateway = get_existing_gateway(gw_port, True, gw_token, False)

    print(". connected")

    # Retrieve spark session, sqlContext and sparkContext
    #

    conf = SparkConf(_jconf=gateway.entry_point.getSparkConf())
    sc = RemoteContext(gateway=gateway, conf=conf)
    if sc.version < "3.0":
        from pyspark.sql import HiveContext  # pylint: disable=import-error,import-outside-toplevel

        sqlContext = HiveContext(sc, gateway.entry_point.getSQLContext())
    else:
        from pyspark.sql import SQLContext  # pylint: disable=import-error,import-outside-toplevel
        from pyspark.sql.session import (  # pylint: disable=import-error,import-outside-toplevel
            SparkSession,
        )

        jsqlContext = gateway.entry_point.getSQLContext()
        sqlContext = SQLContext(sc, SparkSession(sc, jsqlContext.sparkSession()), jsqlContext)

    spark = sqlContext.sparkSession
    sc = spark.sparkContext

    # Enable pretty printing of dataframes
    #
    spark.conf.set("spark.sql.repl.eagerEval.enabled", "true")

    # Define a separate pool for the fair scheduler
    #
    job_info = JobInfo(sc)

    # Patch the remote spark UI into the _repr_html_ call
    #
    def repr_html(uiWebUrl):
        def sc_repr_html():
            return """
            <div>
                <p><b>SparkContext</b></p>
                <p><a href="{uiWebUrl}">Spark UI</a></p>
                <dl>
                  <dt>Version</dt><dd><code>v{sc.version}</code></dd>
                  <dt>AppName</dt><dd><code>{sc.appName}</code></dd>
                  <dt>Master</dt><dd><code>{sc.master}</code></dd>
                </dl>
            </div>
            """.format(
                sc=spark.sparkContext, uiWebUrl=uiWebUrl
            )

        return sc_repr_html

    sc_repr_html = repr_html(sparkUi)
    sc._repr_html_ = sc_repr_html  # pylint: disable=protected-access

    # Monkey patch Databricks Cli to allow mlflow tracking with the credentials provided
    # by this routine
    # Only necessary when mlflow is installed
    #
    try:
        from databricks_cli.configure.provider import (  # pylint: disable=import-outside-toplevel
            ProfileConfigProvider,
            DatabricksConfig,
        )

        def get_config(self):  # pylint: disable=unused-argument
            config = DatabricksConfig(host, None, None, gw_token, False)
            if config.is_valid:
                return config
            return None

        ProfileConfigProvider.get_config = get_config
    except Exception:  # pylint: disable=broad-except
        pass

    # Initialize the ipython shell with spark context
    #
    shell = get_ipython()
    shell.sc = sc
    shell.sqlContext = sqlContext
    shell.displayHTML = lambda html: display(HTML(html))

    # Retrieve the py4j gateway entrypoint
    #
    entry_point = spark.sparkContext._gateway.entry_point  # pylint: disable=protected-access

    # Initialize dbutils
    #
    dbutils = DbjlUtils(shell, entry_point)

    # Setting up Spark progress bar
    #
    if progressbar:
        # print("Set up Spark progress bar")
        load_progressbar(sc, job_info)

    # Register sql magic
    #
    # ip.register_magic_function(sql, magic_kind="line_cell")
    ip.register_magics(DbjlMagics)

    # Forward spark variables to the user namespace
    #
    ip.user_ns["spark"] = spark
    ip.user_ns["sc"] = sc
    ip.user_ns["sqlContext"] = sqlContext
    ip.user_ns["dbutils"] = dbutils
    ip.user_ns["dbbrowser"] = DatabricksBrowser(spark, dbutils)

    print("The following global variables have been created:")
    print("- spark       Spark session")
    print("- sc          Spark context")
    print("- sqlContext  Hive Context")
    print("- dbutils     Databricks utilities (filesystem access only)")
    print("- dbbrowser   Allows to browse dbfs and databases:")
    print("              - dbbrowser.dbfs()")
    print("              - dbbrowser.databases()\n")

    show_status(spark)
    return None
