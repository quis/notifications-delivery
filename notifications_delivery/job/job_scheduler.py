from apscheduler.schedulers.background import BackgroundScheduler

from notifications_delivery.job.jobs import process_jobs


class JobScheduler(object):

    def __init__(self):
        self.scheduler = BackgroundScheduler()
        self.scheduler.add_job(process_jobs, 'interval', seconds=30)

    def start(self):
        self.scheduler.start()

    def shutdown(self):
        self.scheduler.shutdown(wait=True)
