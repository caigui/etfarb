from dingtalkchatbot.chatbot import DingtalkChatbot
import datetime

class DingTalkNotifier:
    """
    钉钉消息通知，还有微信公账号or其他
    """
    def __init__(self):
        # Initialize webhook and secrets here
        # 填写钉钉机器人的webhook和secret
        self.webhook = 'https://oapi.dingtalk.com/robot/send?access_token=7cc49fb6a3b71f21dd3c0b85db4982067f1fa2eb264e20e4d2b1150d29745999'
        self.secrets = 'SEC10d71ad2ff4ebc9c55c8e542318dbc6ad81a5a2390c29d81bd4b04abb89a2dda'
        # Initialize the DingtalkChatbot with the webhook and optional secrets
        self.dingbot = DingtalkChatbot(self.webhook, secret=self.secrets)

    def send_alert(self, message, title):
        """Send a markdown message to DingTalk group using the DingTalk chatbot"""
        # 11:30 - 13:00无需通知
        try:
            now = datetime.datetime.now().time()  # Extract only the time component
            forbidden_start = datetime.time(12, 30)  # Define forbidden start time
            forbidden_end = datetime.time(13, 0)     # Define forbidden end time

            # 检查时间段是否重叠
            if now < forbidden_start or now >= forbidden_end:
                self.dingbot.send_markdown(
                    title=title,
                    text=message,
                    is_at_all=True  # Message will be sent to all members in the group
                )
                return True  # 没有重叠
            else:
                return False  # 重叠
        except ValueError:
            print("Error: Invalid time format. Please use HH:MM.")
            return False  # 出错也返回 False，避免程序崩溃
      

def main():
    """Main function to demonstrate usage of DingTalkNotifier."""
    notifier = DingTalkNotifier()
    message = "### Alert\nThis is a test alert from DingTalkNotifier."
    title = "Test Alert"
    
    if notifier.send_alert(message, title):
        print("Notification sent successfully.")
    else:
        print("Failed to send notification.")

if __name__ == "__main__":
    main()
