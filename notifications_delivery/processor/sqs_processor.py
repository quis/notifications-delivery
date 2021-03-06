import boto3
import logging

import sys
from itsdangerous import URLSafeSerializer
from notifications_delivery.clients.notify_client.api_client import ApiClient
from notifications_python_client.errors import (HTTP503Error, HTTPError, InvalidResponse)
from notifications_delivery.clients.sms.twilio import (
    TwilioClient, TwilioClientException)
from notifications_delivery.clients.email.aws_ses import (
    AwsSesClient, AwsSesClientException)


class ProcessingError(Exception):
    '''
    Exception used for messages where the content cannot be processed.
    The message will not be returned to the queue.
    '''
    pass


class ExternalConnectionError(Exception):
    '''
    Exception used for messages where connection error occurs with an
    external api.
    The message will be returned to the queue.
    '''
    pass


def _set_up_logger(config):
    turn_off = config.get('TURN_OFF_LOGGING', False)
    logger = logging.getLogger(__name__)
    if not turn_off:
        logger = logging.getLogger('delivery_notification')
        logger.setLevel(config['DELIVERY_LOG_LEVEL'])
        if config['DEBUG']:
            fh = logging.StreamHandler(sys.stdout)
        else:
            fh = logging.FileHandler(config['DELIVERY_LOG_PATH'])
        fh.setLevel(logging.DEBUG)
        formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
        fh.setFormatter(formatter)
        logger.addHandler(fh)
    return logger


def _get_all_queues(config, queue_name_prefix=''):
    """
    Returns a list of all queues for a aws account.
    """
    client = boto3.client('sqs', region_name=config['AWS_REGION'])
    sqs = boto3.resource('sqs', region_name=config['AWS_REGION'])
    return [sqs.Queue(x) for x in client.list_queues(QueueNamePrefix=queue_name_prefix)['QueueUrls']]


def _decrypt_message(config, encrypted_content):
    serializer = URLSafeSerializer(config.get('SECRET_KEY'))
    return serializer.loads(encrypted_content, salt=config.get('DANGEROUS_SALT'))


def _process_message(config, message, twilio_client, aws_ses_client, notify_beta_client):
    content = _decrypt_message(config, message.body)
    type_ = message.message_attributes.get('type').get('StringValue')
    service_id = message.message_attributes.get('service_id').get('StringValue')
    template_id = message.message_attributes.get('template_id').get('StringValue')
    notification_id = message.message_attributes.get('notification_id').get('StringValue')
    job_id = content['job'] if 'job' in content else None
    status = 'failed'
    to = content['to'] if 'to' in content else content['to_address']
    response = None
    try:
        if type_ == 'email':
            try:
                response = aws_ses_client.send_email(
                    content['from_address'],
                    content['to_address'],
                    content['subject'],
                    content['body'])
                status = 'sent'
            except AwsSesClientException as e:
                raise ProcessingError(e)
        elif type_ == 'sms':
            if 'content' in content:
                try:
                    response = twilio_client.send_sms(content, content['content'])
                    status = 'sent'
                except TwilioClientException as e:
                    raise ProcessingError(e)
            elif 'template' in content:
                try:
                    template_response = notify_beta_client.get_template(service_id, template_id)
                except HTTP503Error as e:
                    raise ExternalConnectionError(e)
                except HTTPError as e:
                    raise ProcessingError(e)
                except InvalidResponse as e:
                    raise InvalidResponse(e)
                try:
                    response = twilio_client.send_sms(content, template_response['content'])
                    status = 'sent'
                except TwilioClientException as e:
                    raise ProcessingError(e)
        else:
            error_msg = "Invalid type {} for message id {}".format(
                type_, message.message_attributes.get('notification_id').get('StringValue'))
            raise ProcessingError(error_msg)
    finally:
        if job_id:
            try:
                notify_beta_client.create_notification(
                    service_id=service_id,
                    template_id=template_id,
                    job_id=job_id,
                    to=to,
                    status=status,
                    notification_id=notification_id)
            except HTTP503Error as e:
                raise ExternalConnectionError(e)
            except HTTPError as e:
                raise ProcessingError(e)
            except InvalidResponse as e:
                raise InvalidResponse(e)


def _get_message_id(message):
    # TODO needed because tests and live api return different type objects
    return message.id if getattr(message, 'id', None) else getattr(message, 'message_id', 'n/a')


def process_all_queues(config, queue_name_prefix):
    """
    For each queue on the aws account process one message.
    """
    logger = _set_up_logger(config)
    twilio_client = TwilioClient(config)
    aws_ses_client = AwsSesClient(region=config['AWS_REGION'])
    notify_beta_client = ApiClient(base_url=config['API_HOST_NAME'],
                                   client_id=config['DELIVERY_CLIENT_USER_NAME'],
                                   secret=config['DELIVERY_CLIENT_SECRET'])
    queues = _get_all_queues(config, queue_name_prefix)
    for queue in queues:
        try:
            messages = queue.receive_messages(
                MaxNumberOfMessages=config['PROCESSOR_MAX_NUMBER_OF_MESSAGES'],
                VisibilityTimeout=config['PROCESSOR_VISIBILITY_TIMEOUT'],
                MessageAttributeNames=config['NOTIFICATION_ATTRIBUTES'])
            for message in messages:
                logger.info("Processing message {}".format(_get_message_id(message)))
                to_delete = True
                try:
                    _process_message(config, message, twilio_client, aws_ses_client, notify_beta_client)
                except ProcessingError as e:
                    msg = (
                        "Failed prcessing message from queue {}."
                        " The message will not be returned to the queue.").format(queue.url)
                    logger.error(msg)
                    logger.exception(e)
                    to_delete = True
                except ExternalConnectionError as e:
                    msg = (
                        "Failed prcessing message from queue {}."
                        " The message will be returned to the queue.").format(queue.url)
                    logger.error(msg)
                    logger.exception(e)
                    to_delete = False
                if to_delete:
                    message.delete()
                    logger.info("Deleted message {}".format(_get_message_id(message)))
        except Exception as e:
            logger.error("Unexpected exception processing message from queue {}".format(queue.url))
            logger.exception(e)


def process_notification_job(config):
    try:
        process_all_queues(config, config['NOTIFICATION_QUEUE_PREFIX'])
    except Exception as e:
        # TODO log errors and report to api
        print(e)
