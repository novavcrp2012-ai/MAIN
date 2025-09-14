import random
import logging
import subprocess
import sys
import os
import re
import time
import concurrent.futures
import discord
from discord.ext import commands, tasks
import docker
import asyncio
from discord import app_commands
from discord.ui import View, Button, Select
import psutil
import datetime
import json
from typing import Dict, List, Optional

# Configuration
TOKEN = 'your discord bot token'
SERVER_LIMIT = 3  # Increased limit per user
DATABASE_FILE = 'database.json'  # Changed to JSON for better structure
LOG_FILE = 'bot.log'
ADMIN_IDS = [1360282267804500081]  # Add your admin user IDs here

# Available Docker images with metadata
DOCKER_IMAGES = {
    "ubuntu-22.04": {
        "name": "ubuntu-22.04-with-tmate",
        "display_name": "Ubuntu 22.04",
        "description": "Standard Ubuntu 22.04 with tmate pre-installed",
        "ram": "100GB",
        "cpu": "64cores"
    },
}

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

intents = discord.Intents.default()
intents.messages = True
intents.message_content = True

bot = commands.Bot(command_prefix='/', intents=intents)
client = docker.from_env()

class ImageSelectView(View):
    def __init__(self, user_id: int):
        super().__init__(timeout=60)
        self.user_id = user_id
        self.selected_image = None
        
        # Create a dropdown for image selection
        select = Select(
            placeholder="Choose an OS image...",
            options=[
                discord.SelectOption(
                    label=img["display_name"],
                    description=img["description"],
                    value=img_name
                ) for img_name, img in DOCKER_IMAGES.items()
            ]
        )
        select.callback = self.select_callback
        self.add_item(select)
        
        # Add deploy button
        deploy_button = Button(label="Deploy", style=discord.ButtonStyle.green, emoji="🚀")
        deploy_button.callback = self.deploy_callback
        self.add_item(deploy_button)
    
    async def select_callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("This is not your deployment!", ephemeral=True)
            return
            
        self.selected_image = interaction.data['values'][0]
        img_data = DOCKER_IMAGES[self.selected_image]
        
        embed = discord.Embed(
            title="Image Selected",
            description=f"**{img_data['display_name']}** ready for deployment",
            color=0x00ff00
        )
        embed.add_field(name="Description", value=img_data["description"], inline=False)
        embed.add_field(name="Resources", value=f"{img_data['ram']} RAM | {img_data['cpu']} CPU", inline=False)
        
        await interaction.response.edit_message(embed=embed, view=self)
    
    async def deploy_callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("This is not your deployment!", ephemeral=True)
            return
            
        if not self.selected_image:
            await interaction.response.send_message("Please select an image first!", ephemeral=True)
            return
            
        await interaction.response.defer()
        await create_server_task(interaction, self.selected_image)
        self.stop()

# Database functions
def load_database() -> Dict:
    if not os.path.exists(DATABASE_FILE):
        return {}
    
    with open(DATABASE_FILE, 'r') as f:
        try:
            return json.load(f)
        except json.JSONDecodeError:
            return {}

def save_database(data: Dict):
    with open(DATABASE_FILE, 'w') as f:
        json.dump(data, f, indent=4)

def add_to_database(user_id: str, container_id: str, ssh_command: str, image_name: str):
    data = load_database()
    
    if user_id not in data:
        data[user_id] = []
    
    data[user_id].append({
        "container_id": container_id,
        "ssh_command": ssh_command,
        "image": image_name,
        "created_at": datetime.datetime.now().isoformat(),
        "status": "running"
    })
    
    save_database(data)

def remove_from_database(container_id: str):
    data = load_database()
    
    for user_id, containers in data.items():
        data[user_id] = [c for c in containers if c["container_id"] != container_id]
    
    save_database(data)

def update_container_status(container_id: str, status: str):
    data = load_database()
    
    for user_id, containers in data.items():
        for container in containers:
            if container["container_id"] == container_id:
                container["status"] = status
    
    save_database(data)

from typing import List, Dict

def get_user_containers(user_id: str) -> List[Dict]:
    data = load_database()
    return data.get(str(user_id), [])

def count_user_containers(user_id: str) -> int:
    return len(get_user_containers(user_id))

def get_container_info(container_id: str) -> Optional[Dict]:
    data = load_database()
    
    for user_id, containers in data.items():
        for container in containers:
            if container["container_id"] == container_id:
                return container
    return None

# Docker helper functions
async def get_container_stats(container_id: str) -> Dict:
    try:
        container = client.containers.get(container_id)
        stats = container.stats(stream=False)
        
        cpu_percent = 0.0
        memory_usage = 0
        memory_limit = 0
        
        if 'cpu_stats' in stats and 'precpu_stats' in stats:
            cpu_delta = stats['cpu_stats']['cpu_usage']['total_usage'] - stats['precpu_stats']['cpu_usage']['total_usage']
            system_delta = stats['cpu_stats']['system_cpu_usage'] - stats['precpu_stats']['system_cpu_usage']
            
            if system_delta > 0 and cpu_delta > 0:
                cpu_percent = (cpu_delta / system_delta) * len(stats['cpu_stats']['cpu_usage']['percpu_usage']) * 100
        
        if 'memory_stats' in stats:
            memory_usage = stats['memory_stats'].get('usage', 0)
            memory_limit = stats['memory_stats'].get('limit', 1)
        
        return {
            'cpu_percent': round(cpu_percent, 2),
            'memory_usage': memory_usage,
            'memory_limit': memory_limit,
            'memory_percent': round((memory_usage / memory_limit) * 100, 2) if memory_limit else 0,
            'online': container.status == 'running'
        }
    except Exception as e:
        logger.error(f"Error getting stats for container {container_id}: {e}")
        return None

async def capture_ssh_session_line(process) -> Optional[str]:
    while True:
        output = await process.stdout.readline()
        if not output:
            break
        output = output.decode('utf-8').strip()
        if "ssh session:" in output:
            return output.split("ssh session:")[1].strip()
    return None

async def execute_command(command: str) -> tuple:
    process = await asyncio.create_subprocess_shell(
        command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )
    stdout, stderr = await process.communicate()
    return stdout.decode(), stderr.decode()

# Bot events
@bot.event
async def on_ready():
    change_status.start()
    logger.info(f'Bot is ready. Logged in as {bot.user}')
    await bot.tree.sync()

@tasks.loop(seconds=30)
async def change_status():
    try:
        data = load_database()
        total_instances = sum(len(containers) for containers in data.values())
        
        statuses = [
            f"Managing {total_instances} instances",
            f"with {len(DOCKER_IMAGES)} OS options",
            "Type /help for commands"
        ]
        
        current_status = statuses[int(time.time()) % len(statuses)]
        await bot.change_presence(activity=discord.Game(name=current_status))
    except Exception as e:
        logger.error(f"Failed to update status: {e}")

# Command functions
async def create_server_task(interaction: discord.Interaction, image_name: str):
    user = str(interaction.user.id)
    
    if count_user_containers(user) >= SERVER_LIMIT:
        embed = discord.Embed(
            title="Instance Limit Reached",
            description=f"You can only have {SERVER_LIMIT} instances at a time.",
            color=0xff0000
        )
        await interaction.followup.send(embed=embed)
        return
    
    image_data = DOCKER_IMAGES.get(image_name)
    if not image_data:
        embed = discord.Embed(
            title="Invalid Image",
            description="The selected image is not available.",
            color=0xff0000
        )
        await interaction.followup.send(embed=embed)
        return
    
    # Send initial embed with loading animation
    embed = discord.Embed(
        title=f"🚀 Deploying {image_data['display_name']} Instance",
        description="Creating your instance... This may take a moment.",
        color=0x3498db
    )
    embed.add_field(name="Status", value="🔄 Initializing...", inline=False)
    embed.set_footer(text="This message will update automatically")
    message = await interaction.followup.send(embed=embed)
    
    try:
        # Step 1: Pull the image if not exists
        embed.set_field_at(0, name="Status", value="🔍 Checking Docker image...", inline=False)
        await message.edit(embed=embed)
        
        try:
            client.images.get(image_data['name'])
        except docker.errors.ImageNotFound:
            embed.set_field_at(0, name="Status", value="⬇️ Downloading Docker image...", inline=False)
            await message.edit(embed=embed)
            
            try:
                client.images.pull(image_data['name'])
            except docker.errors.DockerException as e:
                logger.error(f"Error pulling image {image_data['name']}: {e}")
                raise Exception(f"Failed to download Docker image: {e}")
        
        # Step 2: Create container
        embed.set_field_at(0, name="Status", value="🛠️ Creating container...", inline=False)
        await message.edit(embed=embed)
        
        try:
            container = client.containers.run(
                image_data['name'],
                detach=True,
                tty=True,
                mem_limit='6g',  # 6GB memory limit
                cpu_quota=200000,  # Limit CPU usage
                cpu_shares=512,  # CPU priority
                restart_policy={"Name": "on-failure", "MaximumRetryCount": 3}
            )
            container_id = container.id
        except docker.errors.DockerException as e:
            logger.error(f"Error creating container: {e}")
            raise Exception(f"Failed to create container: {e}")
        
        # Step 3: Start tmate session
        embed.set_field_at(0, name="Status", value="🔑 Generating SSH access...", inline=False)
        await message.edit(embed=embed)
        
        try:
            exec_cmd = await asyncio.create_subprocess_exec(
                "docker", "exec", container_id, "tmate", "-F",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            ssh_session_line = await capture_ssh_session_line(exec_cmd)
            
            if not ssh_session_line:
                raise Exception("Failed to generate SSH session")
        except Exception as e:
            logger.error(f"Error generating SSH session: {e}")
            container.stop()
            container.remove()
            raise Exception(f"Failed to generate SSH session: {e}")
        
        # Step 4: Finalize
        add_to_database(user, container_id, ssh_session_line, image_name)
        
        # Create success embed
        success_embed = discord.Embed(
            title=f"✅ {image_data['display_name']} Instance Ready",
            description="Your instance has been successfully deployed!",
            color=0x00ff00
        )
        success_embed.add_field(
            name="SSH Access",
            value=f"```{ssh_session_line}```",
            inline=False
        )
        success_embed.add_field(
            name="Resources",
            value=f"{image_data['ram']} RAM | {image_data['cpu']} CPU",
            inline=True
        )
        success_embed.add_field(
            name="Management",
            value=f"Use `/stop {container_id[:12]}` to stop this instance",
            inline=True
        )
        success_embed.set_footer(text=f"Instance ID: {container_id[:12]}")
        
        # Send to user's DMs
        try:
            await interaction.user.send(embed=success_embed)
        except discord.Forbidden:
            logger.warning(f"Could not send DM to user {interaction.user.id}")
        
        # Update original message
        embed.title = f"✅ Deployment Complete"
        embed.description = f"{image_data['display_name']} instance created successfully!"
        embed.set_field_at(0, name="Status", value="✔️ Completed", inline=False)
        embed.color = 0x00ff00
        embed.add_field(
            name="Next Steps",
            value="Check your DMs for SSH access details!",
            inline=False
        )
        await message.edit(embed=embed)
        
    except Exception as e:
        logger.error(f"Error in deployment: {e}")
        
        error_embed = discord.Embed(
            title="❌ Deployment Failed",
            description=str(e),
            color=0xff0000
        )
        error_embed.add_field(
            name="Status",
            value="Failed - Please try again later",
            inline=False
        )
        
        await message.edit(embed=error_embed)

async def manage_server(interaction: discord.Interaction, action: str, container_id: str):
    user = str(interaction.user.id)
    container_info = get_container_info(container_id)
    
    if not container_info:
        embed = discord.Embed(
            title="Instance Not Found",
            description="No instance found with that ID.",
            color=0xff0000
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return
    
    if container_info['user_id'] != user and interaction.user.id not in ADMIN_IDS:
        embed = discord.Embed(
            title="Permission Denied",
            description="You don't have permission to manage this instance.",
            color=0xff0000
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return
    
    try:
        container = client.containers.get(container_id)
        image_data = DOCKER_IMAGES.get(container_info['image'], {})
        
        if action == "start":
            container.start()
            status = "started"
            update_container_status(container_id, "running")
        elif action == "stop":
            container.stop()
            status = "stopped"
            update_container_status(container_id, "stopped")
        elif action == "restart":
            container.restart()
            status = "restarted"
            update_container_status(container_id, "running")
        elif action == "remove":
            container.stop()
            container.remove()
            remove_from_database(container_id)
            status = "removed"
        else:
            raise ValueError("Invalid action")
        
        embed = discord.Embed(
            title=f"Instance {status.capitalize()}",
            description=f"Instance `{container_id[:12]}` has been {status}.",
            color=0x00ff00
        )
        
        if action != "remove":
            stats = await get_container_stats(container_id)
            if stats:
                embed.add_field(
                    name="Resources",
                    value=f"CPU: {stats['cpu_percent']}% | Memory: {stats['memory_percent']}%",
                    inline=False
                )
        
        await interaction.response.send_message(embed=embed)
        
        if action in ["start", "restart"]:
            # Regenerate SSH session after restart
            try:
                exec_cmd = await asyncio.create_subprocess_exec(
                    "docker", "exec", container_id, "tmate", "-F",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )
                ssh_session_line = await capture_ssh_session_line(exec_cmd)
                
                if ssh_session_line:
                    dm_embed = discord.Embed(
                        title=f"🔑 New SSH Session for {image_data.get('display_name', 'Instance')}",
                        description=f"```{ssh_session_line}```",
                        color=0x00ff00
                    )
                    dm_embed.add_field(
                        name="Instance ID",
                        value=container_id[:12],
                        inline=False
                    )
                    await interaction.user.send(embed=dm_embed)
            except Exception as e:
                logger.error(f"Error regenerating SSH session: {e}")
    
    except docker.errors.NotFound:
        embed = discord.Embed(
            title="Instance Not Found",
            description="The container no longer exists.",
            color=0xff0000
        )
        await interaction.response.send_message(embed=embed)
        remove_from_database(container_id)
    except docker.errors.DockerException as e:
        embed = discord.Embed(
            title="Error Managing Instance",
            description=str(e),
            color=0xff0000
        )
        await interaction.response.send_message(embed=embed)

async def regen_ssh_command(interaction: discord.Interaction, container_id: str):
    user = str(interaction.user.id)
    container_info = get_container_info(container_id)
    
    if not container_info:
        embed = discord.Embed(
            title="Instance Not Found",
            description="No instance found with that ID.",
            color=0xff0000
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return
    
    if container_info['user_id'] != user and interaction.user.id not in ADMIN_IDS:
        embed = discord.Embed(
            title="Permission Denied",
            description="You don't have permission to manage this instance.",
            color=0xff0000
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return
    
    await interaction.response.defer()
    
    try:
        container = client.containers.get(container_id)
        if container.status != 'running':
            raise Exception("Instance is not running")
        
        exec_cmd = await asyncio.create_subprocess_exec(
            "docker", "exec", container_id, "tmate", "-F",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        ssh_session_line = await capture_ssh_session_line(exec_cmd)
        
        if not ssh_session_line:
            raise Exception("Failed to generate SSH session")
        
        # Update the database with new SSH command
        for user_id, containers in load_database().items():
            for container in containers:
                if container["container_id"] == container_id:
                    container["ssh_command"] = ssh_session_line
        save_database(load_database())
        
        image_data = DOCKER_IMAGES.get(container_info['image'], {})
        
        embed = discord.Embed(
            title=f"🔑 New SSH Session for {image_data.get('display_name', 'Instance')}",
            description=f"```{ssh_session_line}```",
            color=0x00ff00
        )
        embed.add_field(
            name="Instance ID",
            value=container_id[:12],
            inline=False
        )
        
        await interaction.user.send(embed=embed)
        await interaction.followup.send(
            embed=discord.Embed(
                description="New SSH session generated. Check your DMs!",
                color=0x00ff00
            )
        )
    
    except Exception as e:
        embed = discord.Embed(
            title="Error Generating SSH Session",
            description=str(e),
            color=0xff0000
        )
        await interaction.followup.send(embed=embed)

async def show_instance_info(interaction: discord.Interaction, container_id: str):
    container_info = get_container_info(container_id)
    
    if not container_info:
        embed = discord.Embed(
            title="Instance Not Found",
            description="No instance found with that ID.",
            color=0xff0000
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return
    
    user = str(interaction.user.id)
    if container_info['user_id'] != user and interaction.user.id not in ADMIN_IDS:
        embed = discord.Embed(
            title="Permission Denied",
            description="You don't have permission to view this instance.",
            color=0xff0000
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return
    
    await interaction.response.defer()
    
    try:
        container = client.containers.get(container_id)
        image_data = DOCKER_IMAGES.get(container_info['image'], {})
        stats = await get_container_stats(container_id)
        
        embed = discord.Embed(
            title=f"{image_data.get('display_name', 'Instance')} Details",
            color=0x3498db
        )
        embed.add_field(
            name="Instance ID",
            value=container_id[:12],
            inline=True
        )
        embed.add_field(
            name="Status",
            value=container.status.capitalize(),
            inline=True
        )
        embed.add_field(
            name="Created",
            value=datetime.datetime.fromisoformat(container_info['created_at']).strftime('%Y-%m-%d %H:%M'),
            inline=True
        )
        
        if stats:
            embed.add_field(
                name="CPU Usage",
                value=f"{stats['cpu_percent']}%",
                inline=True
            )
            embed.add_field(
                name="Memory Usage",
                value=f"{stats['memory_percent']}% ({stats['memory_usage']/1024/1024:.2f}MB/{stats['memory_limit']/1024/1024:.2f}MB)",
                inline=True
            )
        
        if container_info.get('ssh_command'):
            embed.add_field(
                name="SSH Access",
                value=f"```{container_info['ssh_command']}```",
                inline=False
            )
        
        view = View()
        if container.status == 'running':
            stop_button = Button(label="Stop", style=discord.ButtonStyle.red, emoji="⏹️")
            stop_button.callback = lambda i: manage_server(i, "stop", container_id)
            view.add_item(stop_button)
            
            restart_button = Button(label="Restart", style=discord.ButtonStyle.blurple, emoji="🔄")
            restart_button.callback = lambda i: manage_server(i, "restart", container_id)
            view.add_item(restart_button)
        else:
            start_button = Button(label="Start", style=discord.ButtonStyle.green, emoji="▶️")
            start_button.callback = lambda i: manage_server(i, "start", container_id)
            view.add_item(start_button)
        
        ssh_button = Button(label="Regen SSH", style=discord.ButtonStyle.gray, emoji="🔑")
        ssh_button.callback = lambda i: regen_ssh_command(i, container_id)
        view.add_item(ssh_button)
        
        remove_button = Button(label="Remove", style=discord.ButtonStyle.red, emoji="🗑️")
        remove_button.callback = lambda i: manage_server(i, "remove", container_id)
        view.add_item(remove_button)
        
        await interaction.followup.send(embed=embed, view=view)
    
    except docker.errors.NotFound:
        embed = discord.Embed(
            title="Instance Not Found",
            description="The container no longer exists.",
            color=0xff0000
        )
        await interaction.followup.send(embed=embed)
        remove_from_database(container_id)
    except Exception as e:
        embed = discord.Embed(
            title="Error Getting Instance Info",
            description=str(e),
            color=0xff0000
        )
        await interaction.followup.send(embed=embed)

# Slash commands
@bot.tree.command(name="deploy", description="Create a new instance")
async def deploy(interaction: discord.Interaction):
    """Show the image selection GUI for deployment"""
    view = ImageSelectView(interaction.user.id)
    
    embed = discord.Embed(
        title="🚀 Deploy a New Instance",
        description="Select an OS image from the dropdown below:",
        color=0x3498db
    )
    embed.set_footer(text="You have 60 seconds to choose")
    
    await interaction.response.send_message(embed=embed, view=view)

@bot.tree.command(name="start", description="Start your instance")
@app_commands.describe(container_id="The ID of your instance (first 12 chars)")
async def start(interaction: discord.Interaction, container_id: str):
    """Start a stopped instance"""
    await manage_server(interaction, "start", container_id)

@bot.tree.command(name="stop", description="Stop your instance")
@app_commands.describe(container_id="The ID of your instance (first 12 chars)")
async def stop(interaction: discord.Interaction, container_id: str):
    """Stop a running instance"""
    await manage_server(interaction, "stop", container_id)

@bot.tree.command(name="restart", description="Restart your instance")
@app_commands.describe(container_id="The ID of your instance (first 12 chars)")
async def restart(interaction: discord.Interaction, container_id: str):
    """Restart an instance"""
    await manage_server(interaction, "restart", container_id)

@bot.tree.command(name="remove", description="Remove your instance")
@app_commands.describe(container_id="The ID of your instance (first 12 chars)")
async def remove(interaction: discord.Interaction, container_id: str):
    """Remove an instance"""
    await manage_server(interaction, "remove", container_id)

@bot.tree.command(name="regen-ssh", description="Generate a new SSH session")
@app_commands.describe(container_id="The ID of your instance (first 12 chars)")
async def regen_ssh(interaction: discord.Interaction, container_id: str):
    """Regenerate SSH session credentials"""
    await regen_ssh_command(interaction, container_id)

@bot.tree.command(name="info", description="Get info about an instance")
@app_commands.describe(container_id="The ID of your instance (first 12 chars)")
async def info(interaction: discord.Interaction, container_id: str):
    """Get detailed information about an instance"""
    await show_instance_info(interaction, container_id)

@bot.tree.command(name="list", description="List all your instances")
async def list_instances(interaction: discord.Interaction):
    """List all instances owned by the user"""
    user = str(interaction.user.id)
    containers = get_user_containers(user)
    
    if not containers:
        embed = discord.Embed(
            title="No Instances Found",
            description="You don't have any instances yet. Use `/deploy` to create one!",
            color=0xff0000
        )
        await interaction.response.send_message(embed=embed)
        return
    
    embed = discord.Embed(
        title="Your Instances",
        description=f"You have {len(containers)}/{SERVER_LIMIT} instances",
        color=0x3498db
    )
    
    for container in containers:
        image_data = DOCKER_IMAGES.get(container['image'], {})
        status = container.get('status', 'unknown').capitalize()
        
        embed.add_field(
            name=f"{image_data.get('display_name', 'Instance')} ({container['container_id'][:12]})",
            value=f"Status: {status}\nCreated: {datetime.datetime.fromisoformat(container['created_at']).strftime('%Y-%m-%d')}",
            inline=True
        )
    
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="stats", description="Get system resource statistics")
async def stats(interaction: discord.Interaction):
    """Show system resource usage"""
    await interaction.response.defer()
    
    try:
        # Get host system stats
        cpu_percent = psutil.cpu_percent()
        memory = psutil.virtual_memory()
        disk = psutil.disk_usage('/')
        
        # Get Docker stats
        total_containers = len(client.containers.list(all=True))
        running_containers = len(client.containers.list())
        
        embed = discord.Embed(
            title="System Statistics",
            color=0x3498db
        )
        embed.add_field(
            name="CPU Usage",
            value=f"{cpu_percent}%",
            inline=True
        )
        embed.add_field(
            name="Memory Usage",
            value=f"{memory.percent}% ({memory.used/1024/1024:.0f}MB/{memory.total/1024/1024:.0f}MB)",
            inline=True
        )
        embed.add_field(
            name="Disk Usage",
            value=f"{disk.percent}% ({disk.used/1024/1024:.0f}MB/{disk.total/1024/1024:.0f}MB)",
            inline=True
        )
        embed.add_field(
            name="Docker Containers",
            value=f"{running_containers}/{total_containers} running",
            inline=True
        )
        
        await interaction.followup.send(embed=embed)
    
    except Exception as e:
        embed = discord.Embed(
            title="Error Getting Statistics",
            description=str(e),
            color=0xff0000
        )
        await interaction.followup.send(embed=embed)

@bot.tree.command(name="help", description="Show help information")
async def help_command(interaction: discord.Interaction):
    """Show help message"""
    embed = discord.Embed(
        title="Instance Manager Help",
        description="Manage your Docker instances through Discord",
        color=0x3498db
    )
    
    embed.add_field(
        name="/deploy",
        value="Create a new instance with a graphical interface",
        inline=False
    )
    embed.add_field(
        name="/list",
        value="List all your instances",
        inline=False
    )
    embed.add_field(
        name="/info <id>",
        value="Get detailed information about an instance",
        inline=False
    )
    embed.add_field(
        name="/start <id>",
        value="Start a stopped instance",
        inline=False
    )
    embed.add_field(
        name="/stop <id>",
        value="Stop a running instance",
        inline=False
    )
    embed.add_field(
        name="/restart <id>",
        value="Restart an instance",
        inline=False
    )
    embed.add_field(
        name="/regen-ssh <id>",
        value="Generate new SSH credentials",
        inline=False
    )
    embed.add_field(
        name="/remove <id>",
        value="Permanently remove an instance",
        inline=False
    )
    embed.add_field(
        name="/stats",
        value="Show system resource usage",
        inline=False
    )
    
    await interaction.response.send_message(embed=embed)

# Admin commands
@bot.tree.command(name="admin-list", description="[ADMIN] List all instances")
async def admin_list(interaction: discord.Interaction):
    """Admin command to list all instances"""
    if interaction.user.id not in ADMIN_IDS:
        embed = discord.Embed(
            title="Permission Denied",
            description="This command is for admins only.",
            color=0xff0000
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return
    
    data = load_database()
    total_instances = sum(len(containers) for containers in data.values())
    
    embed = discord.Embed(
        title="All Instances",
        description=f"There are {total_instances} instances in total",
        color=0x3498db
    )
    
    for user_id, containers in data.items():
        user = await bot.fetch_user(int(user_id))
        username = user.name if user else f"Unknown User ({user_id})"
        
        embed.add_field(
            name=username,
            value=f"{len(containers)} instances",
            inline=True
        )
    
    await interaction.response.send_message(embed=embed)

bot.run(TOKEN)