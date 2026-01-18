from django.core.management.base import BaseCommand
from datetime import datetime

class Command(BaseCommand):
    help = 'Train TensorFlow arrival predictor using CollectionHistory data.'

    def add_arguments(self, parser):
        parser.add_argument('--route', type=int, default=None, help='Train for a specific route id')
        parser.add_argument('--epochs', type=int, default=8)
        parser.add_argument('--batch', type=int, default=1024)

    def handle(self, *args, **options):
        from gtrack.tf_predictor import TensorFlowArrivalPredictor
        route_id = options.get('route')
        epochs = options.get('epochs')
        batch = options.get('batch')
        self.stdout.write(self.style.NOTICE(f"Starting training. route={route_id} epochs={epochs} batch={batch}"))
        tfp = TensorFlowArrivalPredictor()
        ok = tfp.train(route_id=route_id, epochs=epochs, batch_size=batch)
        if ok:
            self.stdout.write(self.style.SUCCESS('Training completed and model saved.'))
        else:
            self.stdout.write(self.style.ERROR('Training failed.'))