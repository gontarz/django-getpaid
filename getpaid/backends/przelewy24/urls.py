from django.conf.urls import url
from django.views.decorators.csrf import csrf_exempt
from getpaid.backends.przelewy24.views import StatusView, ReturnView

urlpatterns = [
    url(r'^status/$',
        csrf_exempt(StatusView.as_view()),
        name='status'),
    url(r'^return/(?P<pk>\d+)/',
        csrf_exempt(ReturnView.as_view()),
        name='return'),
]
