import * as path from 'path';
import * as cdk from 'aws-cdk-lib';
import { Construct } from 'constructs';
import { Duration, RemovalPolicy, CfnOutput } from 'aws-cdk-lib';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as sfn from 'aws-cdk-lib/aws-stepfunctions';
import * as tasks from 'aws-cdk-lib/aws-stepfunctions-tasks';
import * as events from 'aws-cdk-lib/aws-events';
import * as targets from 'aws-cdk-lib/aws-events-targets';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as sns from 'aws-cdk-lib/aws-sns';
import * as subs from 'aws-cdk-lib/aws-sns-subscriptions';
import * as cw from 'aws-cdk-lib/aws-cloudwatch';

export class InfraStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props?: cdk.StackProps) {
    super(scope, id, props);

    // S3: artifacts / scratch
    const artifactsBucket = new s3.Bucket(this, 'ArtifactsBucket', {
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      encryption: s3.BucketEncryption.S3_MANAGED,
      removalPolicy: RemovalPolicy.DESTROY,        // dev only
      autoDeleteObjects: true                      // dev only
    });

    // Log group for app/system logs
    const appLogs = new logs.LogGroup(this, 'AppLogGroup', {
      logGroupName: '/siren/app',
      retention: logs.RetentionDays.ONE_MONTH,
      removalPolicy: RemovalPolicy.DESTROY
    });

    // Helper to make Python Lambdas
    const mkPy = (name: string, relDir: string, memoryMB = 256) => {
      const fn = new lambda.Function(this, name, {
        runtime: lambda.Runtime.PYTHON_3_12,
        handler: 'handler.handler',
        code: lambda.Code.fromAsset(path.join(__dirname, `../../${relDir}`)),
        timeout: Duration.seconds(30),
        memorySize: memoryMB,
        logRetention: logs.RetentionDays.ONE_WEEK,
        environment: {
          ARTIFACTS_BUCKET: artifactsBucket.bucketName,
          APP_LOG_GROUP: appLogs.logGroupName
        }
      });
      artifactsBucket.grantReadWrite(fn);
      appLogs.grantWrite(fn);
      return fn;
    };

    // Lambda stubs
    const metricsGet   = mkPy('MetricsGetFn',   'lambdas/metrics_get');
    metricsGet.addEnvironment('DATA_KEY', 'data/system_performance_metrics.csv');
    metricsGet.addEnvironment('SAMPLE_ROWS', '30');
    const logsQuery    = mkPy('LogsQueryFn',    'lambdas/logs_query', 256);
    logsQuery.addEnvironment('INCIDENTS_KEY', 'data/incident_event_log.csv');
    logsQuery.addEnvironment('MAX_ROWS', '50000');
    const agentInvoke  = mkPy('AgentInvokeFn',  'lambdas/agent_invoke', 256);
    // Sydney + Anthropic defaults (Haiku now; change env to switch models)
    agentInvoke.addEnvironment('BEDROCK_REGION', 'ap-southeast-2');
    agentInvoke.addEnvironment('BEDROCK_MODEL_ID', 'anthropic.claude-3-haiku-20240307-v1:0');
    // allow invoking Anthropic models in Sydney (scope to specific model ARNs)
    agentInvoke.addToRolePolicy(new iam.PolicyStatement({
      actions: ['bedrock:InvokeModel'],
      resources: [
        'arn:aws:bedrock:ap-southeast-2::foundation-model/anthropic.claude-3-haiku-20240307-v1:0',
        'arn:aws:bedrock:ap-southeast-2::foundation-model/anthropic.claude-3-sonnet-20240229-v1:0',
        'arn:aws:bedrock:ap-southeast-2::foundation-model/anthropic.claude-3-5-sonnet-20240620-v1:0',
        'arn:aws:bedrock:ap-southeast-2::foundation-model/anthropic.claude-3-7-sonnet-20250219-v1:0'
      ]
    }));
    const runSSM       = mkPy('RunSSMFn',       'lambdas/run_ssm');
    const notifySlack  = mkPy('NotifySlackFn',  'lambdas/notify_slack');
    const approvalCheck= mkPy('ApprovalCheckFn','lambdas/approval_check');

    const sirenTopic = new sns.Topic(this, 'SirenTopic', {
      displayName: 'SIREN Posture Summaries'
    });
    sirenTopic.addSubscription(new subs.EmailSubscription('sadmansakib99876@gmail.com'));
    sirenTopic.grantPublish(notifySlack);
    notifySlack.addEnvironment('SNS_TOPIC_ARN', sirenTopic.topicArn);

    const dashboard = new cw.Dashboard(this, 'SirenDashboard', {
      dashboardName: 'SIREN-Posture'
    });
    dashboard.addWidgets(new cw.GraphWidget({
      title: 'Rows Scanned (logs_query)',
      left: [
        new cw.Metric({
          namespace: 'SIREN/App',
          metricName: 'RowsScanned',
          dimensionsMap: { Stage: 'logs_query' },
          statistic: 'Sum',
          period: Duration.minutes(5)
        })
      ]
    }));
    dashboard.addWidgets(new cw.GraphWidget({
      title: 'LLM Latency p95 (agent_invoke)',
      left: [
        new cw.Metric({
          namespace: 'SIREN/App',
          metricName: 'SummarizerLatencyMs',
          dimensionsMap: { Stage: 'agent_invoke' },
          statistic: 'p95',
          period: Duration.minutes(5)
        })
      ]
    }));

    // Step Functions: simple demo chain
    const tMetrics = new tasks.LambdaInvoke(this, 'MetricsGet', {
      lambdaFunction: metricsGet,
      outputPath: '$.Payload'
    });
    tMetrics.addRetry({
      errors: ['Lambda.ServiceException', 'Lambda.AWSLambdaException', 'States.TaskFailed'],
      interval: Duration.seconds(2),
      backoffRate: 2.0,
      maxAttempts: 3
    });
    const tLogs = new tasks.LambdaInvoke(this, 'LogsQuery', {
      lambdaFunction: logsQuery,
      outputPath: '$.Payload'
    });
    tLogs.addRetry({
      errors: ['Lambda.ServiceException', 'Lambda.AWSLambdaException', 'States.TaskFailed'],
      interval: Duration.seconds(2),
      backoffRate: 2.0,
      maxAttempts: 3
    });
    const tAgent = new tasks.LambdaInvoke(this, 'AgentInvoke', {
      lambdaFunction: agentInvoke,
      outputPath: '$.Payload'
    });
    tAgent.addRetry({
      errors: ['Lambda.ServiceException', 'Lambda.AWSLambdaException', 'States.TaskFailed'],
      interval: Duration.seconds(2),
      backoffRate: 2.0,
      maxAttempts: 2
    });
    const tApproval = new tasks.LambdaInvoke(this, 'RequestApproval', {
      lambdaFunction: notifySlack,
      integrationPattern: sfn.IntegrationPattern.WAIT_FOR_TASK_TOKEN,
      payload: sfn.TaskInput.fromObject({
        summary: sfn.JsonPath.stringAt('$.llm_text'),
        taskToken: sfn.JsonPath.taskToken
      }),
      resultPath: '$.approval',
      timeout: Duration.hours(1)
    });
    const tNotify = new tasks.LambdaInvoke(this, 'NotifySlack', {
      lambdaFunction: notifySlack,
      outputPath: '$.Payload'
    });
    tNotify.addRetry({
      errors: ['Lambda.ServiceException', 'Lambda.AWSLambdaException', 'States.TaskFailed'],
      interval: Duration.seconds(2),
      backoffRate: 2.0,
      maxAttempts: 3
    });

    const chain = tMetrics.next(tLogs).next(tAgent).next(tApproval).next(tNotify);

    const sfnLogs = new logs.LogGroup(this, 'SfnLogs', {
      retention: logs.RetentionDays.ONE_WEEK,
      removalPolicy: RemovalPolicy.DESTROY
    });

    const stateMachine = new sfn.StateMachine(this, 'SirenStateMachine', {
      definitionBody: sfn.DefinitionBody.fromChainable(chain),
      timeout: Duration.hours(2),
      logs: {
        destination: sfnLogs,
        level: sfn.LogLevel.ALL
      }
    });

    // EventBridge: trigger every 5 minutes
    const rule = new events.Rule(this, 'Every5Minutes', {
      schedule: events.Schedule.rate(Duration.minutes(5))
    });
    rule.addTarget(new targets.SfnStateMachine(stateMachine));

    // Useful outputs
    new CfnOutput(this, 'ArtifactsBucketName', { value: artifactsBucket.bucketName });
    new CfnOutput(this, 'AppLogGroupName', { value: appLogs.logGroupName });
    new CfnOutput(this, 'StateMachineArn', { value: stateMachine.stateMachineArn });
  }
}
