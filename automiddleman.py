import discord
from discord.ext import commands
from discord.ui import Button, View
import subprocess
import asyncio
import random
import string
import os
import json
import shutil
import re

# Bot configuration with intents
intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)

TOKEN = "token"  # Replace with your actual bot token

# Constants
EMBED_COLOR_GREEN = 0x3cc171
EMBED_COLOR_ORANGE = 0xff7f00
LOGS_DIR = "logs/threads"  # Directory for thread logs
DB_DIR = "db"
USERS_DIR = os.path.join(DB_DIR, "users")
STATS_FILE = os.path.join(DB_DIR, "stats.json")
thread_data = {}  # Maps thread.id to a custom thread ID

# Global dictionary for storing pending role selections per thread
# Structure: {thread_id: {user_id: {"role": None, "confirmed": False}}}
pending_roles = {}

# Create directories if they do not exist
os.makedirs(LOGS_DIR, exist_ok=True)
os.makedirs(USERS_DIR, exist_ok=True)
os.makedirs(DB_DIR, exist_ok=True)


def generate_id(length=32):
    """Generate a random alphanumeric ID."""
    return ''.join(random.choices(string.ascii_lowercase + string.digits, k=length))

def sanitize_filename(name):
    """Sanitize a string to be safely used as a filename."""
    return re.sub(r'[^\w\.-]', '_', name)

def update_stats(amount):
    """Update the global statistics with a new transaction amount."""
    stats = {}
    if os.path.exists(STATS_FILE):
        with open(STATS_FILE, 'r') as f:
            stats = json.load(f)
    max_deal_num = max([int(k[4:]) for k in stats.keys() if k.startswith('deal')] or [0])
    new_key = f"deal{max_deal_num + 1}"
    stats[new_key] = amount
    with open(STATS_FILE, 'w') as f:
        json.dump(stats, f, indent=4)

def update_user_stats(bot, user_id, amount_sent=0, amount_received=0):
    """Update individual user statistics."""
    user = bot.get_user(user_id)
    if not user:
        return
    username = sanitize_filename(user.name)
    user_file = os.path.join(USERS_DIR, f"{username}.json")
    if os.path.exists(user_file):
        with open(user_file, 'r') as f:
            user_data = json.load(f)
    else:
        user_data = {
            "Amount Received": 0,
            "Amount Sent": 0,
            "Total Volume": 0,
            "Total Deals": 0
        }
    user_data["Amount Sent"] += amount_sent
    user_data["Amount Received"] += amount_received
    user_data["Total Volume"] = user_data["Amount Sent"] + user_data["Amount Received"]
    if amount_sent or amount_received:
        user_data["Total Deals"] += 1
    with open(user_file, 'w') as f:
        json.dump(user_data, f, indent=4)



@bot.event
async def on_ready():
    print(f"Bot connected as {bot.user}")

@bot.event
async def on_interaction(interaction):
    """
    Handle interactions based on the component's custom_id.
    (Role selection buttons are handled by the view callbacks.)
    """
    if interaction.type == discord.InteractionType.component:
        custom_id = interaction.data["custom_id"]
        if custom_id == "create_ticket":
            await handle_create_ticket(interaction)
        elif custom_id == "cancel_deal":
            await handle_cancel_deal(interaction)
        elif custom_id == "accept_deal":
            await handle_accept_deal(interaction)
        elif custom_id == "confirm_funds":
            await handle_confirm_funds(interaction)
        elif custom_id == "release_funds":
            await handle_release_funds(interaction)
        # Role selection buttons ("role_sender", "role_receiver", "confirm_role")
        # are handled by the RoleSelectionView callbacks.

# ----------------- Roles selections -----------------
class RoleSelectionView(View):
    """View for selecting and confirming roles using buttons."""
    def __init__(self, thread_id):
        super().__init__(timeout=300)
        self.thread_id = thread_id

    @discord.ui.button(label="I am Sender", style=discord.ButtonStyle.primary, custom_id="role_sender")
    async def sender_button(self, interaction: discord.Interaction, button: Button):
        if interaction.user.id not in pending_roles[self.thread_id]:
            await interaction.response.send_message("You are not a participant in this transaction.", ephemeral=True)
            return
        pending_roles[self.thread_id][interaction.user.id]["role"] = "sender"
        pending_roles[self.thread_id][interaction.user.id]["confirmed"] = False  # Reset confirmation if role changes
        await interaction.response.send_message("You have selected **Sender**. Please confirm your selection using the 'Confirm My Role' button.", ephemeral=True)

    @discord.ui.button(label="I am Receiver", style=discord.ButtonStyle.primary, custom_id="role_receiver")
    async def receiver_button(self, interaction: discord.Interaction, button: Button):
        if interaction.user.id not in pending_roles[self.thread_id]:
            await interaction.response.send_message("You are not a participant in this transaction.", ephemeral=True)
            return
        pending_roles[self.thread_id][interaction.user.id]["role"] = "receiver"
        pending_roles[self.thread_id][interaction.user.id]["confirmed"] = False
        await interaction.response.send_message("You have selected **Receiver**. Please confirm your selection using the 'Confirm My Role' button.", ephemeral=True)

    @discord.ui.button(label="Confirm My Role", style=discord.ButtonStyle.success, custom_id="confirm_role")
    async def confirm_button(self, interaction: discord.Interaction, button: Button):
        if interaction.user.id not in pending_roles[self.thread_id]:
            await interaction.response.send_message("You are not a participant in this transaction.", ephemeral=True)
            return
        if pending_roles[self.thread_id][interaction.user.id]["role"] is None:
            await interaction.response.send_message("Please select a role first.", ephemeral=True)
            return

        pending_roles[self.thread_id][interaction.user.id]["confirmed"] = True
        await interaction.response.send_message(f"Your role **{pending_roles[self.thread_id][interaction.user.id]['role']}** has been confirmed.", ephemeral=True)

        # Check if both participants have confirmed their roles
        selections = pending_roles[self.thread_id]
        if all(info["confirmed"] for info in selections.values()):
            roles = {uid: info["role"] for uid, info in selections.items()}
            # Ensure that the two participants have chosen complementary roles
            if len(set(roles.values())) != 2:
                await interaction.followup.send("Error: Both participants have selected the same role. Please reselect your roles.", ephemeral=True)
                # Reset selections
                for uid in selections:
                    selections[uid]["role"] = None
                    selections[uid]["confirmed"] = False
                return

            # Assign roles
            sender_id = None
            receiver_id = None
            for uid, role in roles.items():
                if role == "sender":
                    sender_id = uid
                elif role == "receiver":
                    receiver_id = uid

            # Update the thread's info file (without changing the thread name)
            thread = interaction.channel
            custom_thread_id = thread_data.get(thread.id)
            if custom_thread_id:
                thread_folder = os.path.join(LOGS_DIR, custom_thread_id)
                info_path = os.path.join(thread_folder, "info.json")
                with open(info_path, "r") as f:
                    thread_info = json.load(f)
                thread_info["sender"] = sender_id
                thread_info["receiver"] = receiver_id
                with open(info_path, "w") as f:
                    json.dump(thread_info, f, indent=4)

            await interaction.followup.send("Roles have been successfully confirmed! You may now proceed with the transaction.", ephemeral=False)
            self.stop()  # Disable further interactions

# ----------------- Ticket and Transaction  -----------------
async def handle_create_ticket(interaction):
    """Create a new private thread ticket for the escrow transaction."""
    await interaction.response.defer(ephemeral=True)
    custom_thread_id = generate_id()
    guild = interaction.guild
    channel = interaction.channel
    thread_name = f"Escrow | {interaction.user.name}"
    thread = await channel.create_thread(
        name=thread_name,
        type=discord.ChannelType.private_thread,
        invitable=False
    )
    # Send the custom thread ID so it can be referenced later
    await thread.send(f"Thread ID: ```{custom_thread_id}```")
    await thread.send(f"{interaction.user.mention}")
    thread_data[thread.id] = custom_thread_id
    go_to_thread_button = Button(
        label="Go to thread",
        style=discord.ButtonStyle.link,
        url=thread.jump_url
    )
    view = View()
    view.add_item(go_to_thread_button)
    await interaction.followup.send(
        f"Ticket created: {thread.mention} | ID: `{custom_thread_id}`",
        view=view,
        ephemeral=True
    )
    # Create a logs folder for this thread and write an initial info.json file
    thread_folder = os.path.join(LOGS_DIR, custom_thread_id)
    os.makedirs(thread_folder, exist_ok=True)
    with open(os.path.join(thread_folder, "info.json"), "w") as f:
        json.dump({}, f, indent=4)
    # Send warning and contact messages
    await thread.send(
        "WARNING: Please use only this thread for all transaction-related conversations. "
        "Our bots and staff will never contact you via DM."
    )
    await thread.send(
        "For any questions, contact via LinkdIn"
    )
    # Provide Accept and Cancel buttons
    confirm_view = View()
    confirm_view.add_item(Button(label="Accept", style=discord.ButtonStyle.green, custom_id="accept_deal"))
    confirm_view.add_item(Button(label="Cancel", style=discord.ButtonStyle.red, custom_id="cancel_deal"))
    await thread.send("Do you want to proceed?", view=confirm_view)

async def handle_cancel_deal(interaction):
    """Cancel the transaction and delete the thread along with its logs."""
    await interaction.response.defer()
    thread = interaction.channel
    custom_thread_id = thread_data.get(thread.id)
    await thread.delete()
    if custom_thread_id:
        thread_folder = os.path.join(LOGS_DIR, custom_thread_id)
        if os.path.exists(thread_folder):
            shutil.rmtree(thread_folder)
        thread_data.pop(thread.id, None)

async def handle_accept_deal(interaction):
    """
    Handle deal acceptance:
      - Add the second user to the thread.
      - Initiate role selection via buttons.
      - Generate a Litecoin address using local subprocess commands.
      - Retrieve the private key and update the thread info.
    """
    await interaction.response.defer()
    thread = interaction.channel
    guild = interaction.guild
    # Delete bot messages
    async for message in thread.history(limit=100):
        if message.author == bot.user and not message.content.startswith("Thread ID:"):
            await message.delete()
    # Ask for the Discord ID of the second user
    await thread.send(f"{interaction.user.mention}, please provide the Discord ID (numeric) of the second user.")
    
    def check(msg):
        return msg.channel == thread and msg.author == interaction.user and msg.content.isdigit()
    
    try:
        msg = await bot.wait_for("message", check=check, timeout=60)
        second_user_id = int(msg.content)
        second_user = guild.get_member(second_user_id)
        if not second_user:
            await thread.send("The provided ID does not belong to a valid member of the server.")
            return
        await thread.add_user(second_user)
    except asyncio.TimeoutError:
        await thread.send("Timeout. Closing the ticket.")
        await thread.delete()
        return

    # Initialize role selection for both participants using buttons
    pending_roles[thread.id] = {
        interaction.user.id: {"role": None, "confirmed": False},
        second_user.id: {"role": None, "confirmed": False}
    }
    await thread.send(
        "Both participants, please select your role using the buttons below, then confirm your selection using 'Confirm My Role'.",
        view=RoleSelectionView(thread.id)
    )

    # Generate a new Litecoin address using local commands
    try:
        result = subprocess.run(
            ["litecoin-cli", "getnewaddress"],
            capture_output=True,
            text=True,
            check=True
        )
        address = result.stdout.strip()
        await thread.send(f"Here is your unique Litecoin address: `{address}`")
    except subprocess.CalledProcessError as e:
        await thread.send("Error generating Litecoin address.")
        print(f"Error generating Litecoin address: {e}")
        return
    
    # Provide a button to confirm that funds have been sent
    confirm_view = View()
    confirm_view.add_item(Button(label="Have you sent funds?", style=discord.ButtonStyle.primary, custom_id="confirm_funds"))
    await thread.send(view=confirm_view)
    
    # Retrieve the private key for the generated address
    try:
        privkey_result = subprocess.run(
            ["litecoin-cli", "dumpprivkey", address],
            capture_output=True,
            text=True,
            check=True
        )
        private_key = privkey_result.stdout.strip()
    except subprocess.CalledProcessError as e:
        await thread.send("Error retrieving private key.")
        print(f"Error retrieving private key: {e}")
        return

    custom_thread_id = thread_data.get(thread.id)
    thread_folder = os.path.join(LOGS_DIR, custom_thread_id)
    info_path = os.path.join(thread_folder, "info.json")
    with open(info_path, 'r') as f:
        thread_info = json.load(f)
    thread_info["address"] = address
    thread_info["private_key"] = private_key
    with open(info_path, 'w') as f:
        json.dump(thread_info, f, indent=4)

async def handle_confirm_funds(interaction):
    """
    Monitor the Litecoin address for incoming funds.
    Once funds are detected, update the thread info and wait for confirmations.
    """
    await interaction.response.defer()
    thread = interaction.channel
    address = None
    async for message in thread.history(limit=100):
        if "litecoin address" in message.content.lower():
            # Assume the address is between backticks
            parts = message.content.split('`')
            if len(parts) >= 2:
                address = parts[1]
                break
    if not address:
        await thread.send("Address not found.")
        return
    # Check for funds periodically
    while True:
        try:
            result = subprocess.run(
                ["litecoin-cli", "getreceivedbyaddress", address, "0"],
                capture_output=True,
                text=True,
                check=True
            )
            balance = float(result.stdout.strip())
        except subprocess.CalledProcessError:
            balance = 0
        except ValueError:
            balance = 0

        if balance > 0:
            custom_thread_id = thread_data.get(thread.id)
            thread_folder = os.path.join(LOGS_DIR, custom_thread_id)
            info_path = os.path.join(thread_folder, "info.json")
            with open(info_path, 'r') as f:
                thread_info = json.load(f)
            thread_info["amount"] = balance
            with open(info_path, 'w') as f:
                json.dump(thread_info, f, indent=4)
            await thread.send("💸 Funds received!")
            break
        await asyncio.sleep(10)
    
    await thread.send("⏳ Waiting for confirmations...")
    while True:
        try:
            result = subprocess.run(
                ["litecoin-cli", "getreceivedbyaddress", address, "1"],
                capture_output=True,
                text=True,
                check=True
            )
            conf_balance = float(result.stdout.strip())
        except subprocess.CalledProcessError:
            conf_balance = 0
        except ValueError:
            conf_balance = 0

        if conf_balance > 0:
            await thread.send("✅ Transaction confirmed!")
            break
        await asyncio.sleep(10)
    
    release_view = View()
    release_view.add_item(Button(label="Release funds", style=discord.ButtonStyle.green, custom_id="release_funds"))
    await thread.send(view=release_view)

async def handle_release_funds(interaction):
    """
    Release funds by:
      - Creating, signing, and broadcasting the transaction.
      - Updating the statistics after a successful release.
    """
    await interaction.response.defer()
    thread = interaction.channel
    try:
        address = None
        async for message in thread.history(limit=100):
            if "litecoin address" in message.content.lower():
                parts = message.content.split('`')
                if len(parts) >= 2:
                    address = parts[1]
                    break
        if not address:
            await thread.send("Address not found.")
            return
        await thread.send("Please provide the destination address:")
        
        def check_ltc_address(msg):
            return msg.channel == thread and len(msg.content) in [34, 43, 63]
        
        msg = await bot.wait_for("message", check=check_ltc_address, timeout=60)
        recipient_address = msg.content
        
        # Get the private key for the address
        try:
            privkey_result = subprocess.run(
                ["litecoin-cli", "dumpprivkey", address],
                capture_output=True,
                text=True,
                check=True
            )
            private_key = privkey_result.stdout.strip()
        except subprocess.CalledProcessError as e:
            await thread.send("Error retrieving private key.")
            print(f"Error retrieving private key: {e}")
            return

        # List unspent outputs for the address
        unspent_cmd = ["litecoin-cli", "listunspent", "1", "9999999", f'["{address}"]']
        try:
            unspent_result = subprocess.run(
                unspent_cmd,
                capture_output=True,
                text=True,
                check=True
            )
            unspent_data = json.loads(unspent_result.stdout)
            utxo = unspent_data[0]
        except Exception as e:
            await thread.send("Error retrieving unspent outputs.")
            print(f"Error retrieving unspent outputs: {e}")
            return

        total_amount = float(utxo['amount'])
        txid = utxo['txid']
        vout = utxo['vout']

        # Create raw transaction
        create_tx_cmd = (
            f"litecoin-cli createrawtransaction "
            f"'[{{\"txid\":\"{txid}\",\"vout\":{vout}}}]' "
            f"'{{\"{recipient_address}\":{total_amount}}}'"
        )
        try:
            raw_tx_result = subprocess.run(
                create_tx_cmd,
                shell=True,
                capture_output=True,
                text=True,
                check=True
            )
            raw_hex = raw_tx_result.stdout.strip()
        except subprocess.CalledProcessError as e:
            await thread.send("Error creating raw transaction.")
            print(f"Error creating raw transaction: {e}")
            return

        # Sign the raw transaction
        sign_cmd = (
            f"litecoin-cli signrawtransactionwithkey "
            f"\"{raw_hex}\" "
            f"'[\"{private_key}\"]' | jq -r .hex"
        )
        try:
            signed_result = subprocess.run(
                sign_cmd,
                shell=True,
                capture_output=True,
                text=True,
                check=True
            )
            signed_hex = signed_result.stdout.strip()
        except subprocess.CalledProcessError as e:
            await thread.send("Error signing transaction.")
            print(f"Error signing transaction: {e}")
            return

        hex_size = len(signed_hex) / 2
        fee = 0.0001 * (hex_size / 1000)
        final_amount = total_amount - fee

        # Create final transaction with adjusted amount
        create_final_tx_cmd = (
            f"litecoin-cli createrawtransaction "
            f"'[{{\"txid\":\"{txid}\",\"vout\":{vout}}}]' "
            f"'{{\"{recipient_address}\":{final_amount}}}'"
        )
        try:
            final_raw_result = subprocess.run(
                create_final_tx_cmd,
                shell=True,
                capture_output=True,
                text=True,
                check=True
            )
            final_raw_hex = final_raw_result.stdout.strip()
        except subprocess.CalledProcessError as e:
            await thread.send("Error creating final raw transaction.")
            print(f"Error creating final raw transaction: {e}")
            return

        final_sign_cmd = (
            f"litecoin-cli signrawtransactionwithkey "
            f"\"{final_raw_hex}\" "
            f"'[\"{private_key}\"]' | jq -r .hex"
        )
        try:
            final_signed_result = subprocess.run(
                final_sign_cmd,
                shell=True,
                capture_output=True,
                text=True,
                check=True
            )
            final_signed_hex = final_signed_result.stdout.strip()
        except subprocess.CalledProcessError as e:
            await thread.send("Error signing final transaction.")
            print(f"Error signing final transaction: {e}")
            return

        # Broadcast the transaction
        try:
            broadcast_result = subprocess.run(
                ["litecoin-cli", "sendrawtransaction", final_signed_hex],
                capture_output=True,
                text=True,
                check=True
            )
            txid_broadcast = broadcast_result.stdout.strip()
            await thread.send(f"✅ Funds released! TXID: `{txid_broadcast}`")
        except subprocess.CalledProcessError as e:
            await thread.send("Error broadcasting transaction.")
            print(f"Error broadcasting transaction: {e}")
            return
        
        # Update statistics
        custom_thread_id = thread_data.get(thread.id)
        thread_folder = os.path.join(LOGS_DIR, custom_thread_id)
        info_path = os.path.join(thread_folder, "info.json")
        with open(info_path, 'r') as f:
            thread_info = json.load(f)
        amount = thread_info.get("amount", 0)
        sender_id = thread_info["sender"]
        receiver_id = thread_info["receiver"]
        
        update_stats(amount)
        update_user_stats(bot, sender_id, amount_sent=amount)
        update_user_stats(bot, receiver_id, amount_received=amount)
        
        await thread.send("📊 Stats updated!")
    except Exception as e:
        await thread.send(f"❌ Error: {str(e)}")
        print(f"Error: {e}")

# ----------------- Bot Commands -----------------
@bot.command()
async def ticket(ctx):
    # First embed with service information
    embed = discord.Embed(
        color=EMBED_COLOR_GREEN
    )
    
    embed.add_field(
        name="**What is an Escrow?**",
        value=( 
            "An escrow is a financial arrangement where a third party holds and regulates the payment of funds required for two parties involved in a given transaction. "
            "AutoMiddleman aims to remove the human aspect of an escrow to avoid scams and human mistakes."
        ),
        inline=False
    )
    
    embed.add_field(
        name="**How do I use AutoMiddleman?**",
        value=( 
            "1. Click the button below to start.\n"
            "2. Enter the user ID of the user you are trading with.\n"
            "3. Select your roles within the trade.\n"
            "4. Once the roles have been assigned and confirmed, the sender will be provided with a Litecoin (LTC) address to send the funds to.\n"
            "5. Once the bot confirms that the funds have been received, you may proceed with the deal.\n"
            "6. Once the trade has been completed and everyone is happy, the sender releases the funds to the receiver, marking the end of the deal.\n"
            "7. If something goes wrong, the receiver can return the funds to the sender."
        ),
        inline=False
    )
    
    embed.add_field(
        name="**Is the bot safe to use?**",
        value=( 
            "Yes, the bot is safe to use. We remove the human aspect of an escrow to avoid scams and human mistakes. "
            "If you deal outside or with a user of Tomato without using the bot, we are not responsible for any losses that may occur."
        ),
        inline=False
    )
    
    embed.add_field(
        name="**AutoMiddleman Fees**",
        value=( 
            "AutoMiddleman is free of charge for any deals under $1. For deals above $1, there is a base fee of $0.10 + 1% of the transaction amount, excluding transaction fees. "
            "Transaction fees are beyond our control but rarely total up to any significant amount (usually less than $0.02)."
        ),
        inline=False
    )
    
    view = View()
    await ctx.send(embed=embed, view=view)
    
    # Second embed with important warning
    embed2 = discord.Embed(
        title="Important Warning",
        description=( 
            "If anyone ever asks you to deal outside of AutoMiddleman, we politely advise you to immediately report them to us. "
            "We do not condone any deals outside of AutoMiddleman nor are we responsible for any losses you may incur from dealing outside of Tomato. "
            "We cannot assure the safety of your funds if you do.\n\n"
            "AutoMiddleman has been designed to be a safe and secure platform for all users. We always advise using the escrow bot for your safety and security. "
            "We do not take responsibility for any losses that occur due to dealing outside of AutoMiddleman with a user within our server.\n\n"
            "Regardless of the user having any form of \"Trusted\", \"Admin\", or \"Staff\" roles, we always advise against dealing outside of the bot. "
            "If you are asked to deal without using the escrow, please report them immediately. Our service is reliable and secure, and there is no reason not to use it."
        ),
        color=0xff7f00  # Orange color
    )
    
    embed2.set_footer(text="To report suspicious activity, contact us immediately.")
    
    # Green button to start Litecoin Escrow
    button = Button(
        label="Litecoin Escrow", 
        style=discord.ButtonStyle.success,
        custom_id="create_ticket"
    )
    
    view2 = View()
    view2.add_item(button)
    
    await ctx.send(embed=embed2, view=view2)

@bot.command()
async def stats(ctx):
    """Display server-wide transaction statistics."""
    try:
        with open(STATS_FILE, 'r') as f:
            stats_data = json.load(f)
    except FileNotFoundError:
        await ctx.send("No transactions recorded.")
        return
    total = sum(stats_data.values())
    await ctx.send(
        f"📈 Server Statistics\n"
        f"Total transactions: {len(stats_data)}\n"
        f"Total volume: {total:.8f} LTC"
    )

@bot.command()
async def userstats(ctx, user_id: int):
    """Display statistics for a specific user."""
    try:
        user = await bot.fetch_user(user_id)
    except discord.NotFound:
        await ctx.send("User not found.")
        return
    user_file = os.path.join(USERS_DIR, f"{sanitize_filename(user.name)}.json")
    if not os.path.exists(user_file):
        await ctx.send("No statistics for this user.")
        return
    with open(user_file, 'r') as f:
        user_data = json.load(f)
    await ctx.send(
        f"📊 Stats for {user.name}\n"
        f"Received: {user_data['Amount Received']:.8f} LTC\n"
        f"Sent: {user_data['Amount Sent']:.8f} LTC\n"
        f"Total Volume: {user_data['Total Volume']:.8f} LTC\n"
        f"Transactions: {user_data['Total Deals']}"
    )

bot.run("token")
