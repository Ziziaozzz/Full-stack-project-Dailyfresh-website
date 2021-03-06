from django.core.paginator import Paginator
from django.shortcuts import render, redirect
from django.urls import reverse
from django.views.generic import View
from django.http import HttpResponse
from django.conf import settings
from django.contrib.auth import authenticate, login, logout
from django_redis import get_redis_connection
from itsdangerous import TimedJSONWebSignatureSerializer as Serializer
from itsdangerous import SignatureExpired

from order.models import OrderInfo, OrderGoods
from user.models import User, Address
from goods.models import GoodsSKU
from celery_tasks.tasks import send_register_active_email
from utils.mixin import LoginRequiredMixin
import re

# RegisterView, ActiveView, LoginView, LogoutView, UserInfoView, UserOrderView, AddressView

class RegisterView(View):

    def get(self, request):
        return render(request, "register.html")

    def post(self, request):
        username = request.POST.get("user_name")
        password = request.POST.get("pwd")
        email = request.POST.get("email")
        allow = request.POST.get("allow")

        if not all([username, password, email]):
            return render(request, "register.html", {"errmsg": "Incomplete user info"})

        if not re.match(r"^[a-zA-Z0-9_-]+@[a-zA-Z0-9_-]+(\.[a-zA-Z0-9_-]+)+$", email):
            return render(request, "register.html", {"errmsg": "Incorrect email format"})

        if allow != "on":
            return render(request, "register.html", {"errmsg": "Please accept the agreement."})

        try:
            User.objects.get(username=username)
        except User.DoesNotExist:
            User.username = None

        if User.username:
            return render(request, "register.html", {"errmsg": "Existing user name"})

        user = User.objects.create_user(username, email, password)
        user.is_active = 0
        user.save()

        serializer = Serializer(settings.SECRET_KEY, 3600)
        info = {"confirm": user.id}
        token = serializer.dumps(info).decode("utf8")

        send_register_active_email.delay(email, username, token)
        return redirect(reverse("goods:index"))


class ActiveView(View):

    def get(self, request, token):

        serializer = Serializer(settings.SECRET_KEY, 3600)
        try:
            info = serializer.loads(token)
            user_id = info["confirm"]

            user = User.objects.get(id=user_id)
            user.is_active = 1
            user.save()
            return redirect(reverse("user:login"))

        except SignatureExpired as e:
            return HttpResponse("The activation link is expired.")

        
class LoginView(View):
    def get(self, request):

        if "username" in request.COOKIES:
            username = request.COOKIES.get("username")
            checked = "checked"
        else:
            username = ""
            checked = ""
        return render(request, "login.html", {"username": username, "checked": checked})

    def post(self, request):

        username = request.POST.get("username")
        password = request.POST.get("pwd")

        if not all([username, password]):
            return render(request, "login.html", {"errmsg": "Incomplete data"})

        user = authenticate(username=username, password=password)
        if user is not None:
            if user.is_active:
                login(request, user)

                next_url = request.GET.get("next", reverse("goods:index"))
                response = redirect(next_url)  # HttpResponseRedirect

                remember = request.POST.get("remember")
                if remember == "on":
                    response.set_cookie("username", username, max_age=24 * 3600)
                else:
                    response.delete_cookie(username)
                return response
            else:
                return render(request, "login.html", {"errmsg": "The account is not activated."})

        else:
            return render(request, "login.html", {"errmsg": "Invalid user name or password"})


class LogoutView(View):
    def get(self, request):

        logout(request)
        return redirect(reverse("goods:index"))


class UserInfoView(LoginRequiredMixin, View):

    def get(self, request):

        user = request.user
        address = Address.objects.get_default_address(user)

        con = get_redis_connection("default")
        history_key = "history_%d" % user.id

        sku_ids = con.lrange(history_key, 0, 4)
        goods_li = []
        for id in sku_ids:
            goods = GoodsSKU.objects.get(id=id)
            goods_li.append(goods)

        context = {"page": "user", "address": address, "goods_li": goods_li}
        return render(request, "user_center_info.html", context)


class UserOrderView(LoginRequiredMixin, View):

    def get(self, request, page):

        user = request.user
        orders = OrderInfo.objects.filter(user=user).order_by("-create_time")

        for order in orders:
            order_skus = OrderGoods.objects.filter(order_id=order.order_id)

            for order_sku in order_skus:
                amount = order_sku.count * order_sku.price
                order_sku.amount = amount
            order.status_name = OrderInfo.ORDER_STATUS[order.order_status]
            order.order_skus = order_skus

        paginator = Paginator(orders, 1)

        try:
            page = int(page)
        except Exception as e:
            page = 1

        if page > paginator.num_pages:
            page = 1

        order_page = paginator.page(page)

        num_pages = paginator.num_pages
        if num_pages < 5:
            pages = range(1, num_pages + 1)
        elif page <= 3:
            pages = range(1, 6)
        elif num_pages - page <= 2:
            pages = range(num_pages - 4, num_pages + 1)
        else:
            pages = range(page - 2, page + 3)

        print(order_page)
        context = {"order_page": order_page, "pages": pages, "page": "order"}

        return render(request, "user_center_order.html", context)

    
class AddressView(LoginRequiredMixin, View):

    def get(self, request):

        user = request.user

        default_address = Address.objects.get_default_address(user)

        all_address = Address.objects.get_all_address(user)

        context = {
            "address": default_address,
            "have_address": all_address,
            "page": "address",
        }

        return render(request, "user_center_site.html", context)

    def post(self, request):

        receiver = request.POST.get("receiver")
        addr = request.POST.get("addr")
        zip_code = request.POST.get("zip_code")
        phone = request.POST.get("phone")

        if not all([receiver, addr, phone]):
            return render(request, "user_center_site.html", {"errmsg": "Incomplete data"})

        if not re.match(r"1[3,4,5,7,8]\d{9}$", phone):
            return render(request, "user_center_site.html", {"errmsg": "Incorrect format of phone number"})

        if len(zip_code) != 6:
            return render(request, "user_center_site.html", {"errmsg": "Invalid zip code"})

        user = request.user
        address = Address.objects.get_default_address(user)

        if address:
            is_default = False
        else:
            is_default = True

        Address.objects.create(
            user=user,
            receiver=receiver,
            addr=addr,
            zip_code=zip_code,
            phone=phone,
            is_default=is_default,
        )
        return redirect(reverse("user:address"))
