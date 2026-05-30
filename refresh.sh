cat > ~/nse-scanner/refresh.sh << 'EOF'
#!/bin/bash
cd ~/nse-scanner && source venv/bin/activate
echo "Updating DB..."
python3 db_manager.py --update
echo "Restarting services..."
pkill -f price_api.py; pkill -f scheduler.py; pkill -f serve.py; sleep 2
nohup python3 serve.py > logs/serve.log 2>&1 &
nohup python3 price_api.py > logs/price_api.log 2>&1 &
nohup python3 scheduler.py > logs/scheduler.log 2>&1 &
echo "Done! Open http://140.245.221.168:8080"
EOF
chmod +x ~/nse-scanner/refresh.sh
